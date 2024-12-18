import html
import json
import logging
import traceback
from io import BytesIO
from gtts import gTTS
from langdetect import detect, LangDetectException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, Application
from telegram.error import NetworkError, BadRequest
from telegram.constants import ChatAction, ParseMode
from groq_chat.html_format import format_message
from groq_chat.groq_chat import chatbot, generate_response
import asyncio
import os
from groq_chat.filters import AuthFilter, MessageFilter
from dotenv import load_dotenv
from mongopersistence import MongoPersistence

load_dotenv()

SYSTEM_PROMPT_SP = 1
CANCEL_SP = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

persistence = None
if os.getenv("MONGODB_URL"):
    persistence = MongoPersistence(
        mongo_url=os.getenv("MONGODB_URL"),
        db_name="groq-chatbot",
        name_col_user_data="user_data",
        name_col_bot_data="bot_data",
        name_col_chat_data="chat_data",
        name_col_conversations_data="conversations_data",
        create_col_if_not_exist=True,
        ignore_general_data=["cache"],
        update_interval=10,
    )


def new_chat(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("system_prompt") is not None:
        context.user_data["messages"] = [
            {
                "role": "system",
                "content": context.user_data.get("system_prompt"),
            },
        ]
    else:
        context.user_data["messages"] = []


def text_to_wav(text: str, filename: str) -> None:
    """Convert text to a WAV file."""
    try:
        language = detect(text)
    except LangDetectException:
        logging.error("Language detection failed. Defaulting to English.")
        language = "en"

    try:
        tts = gTTS(text=text, lang=language, slow=False)
        tts.save(filename)
        logging.info(f"Audio saved as '{filename}'")
    except Exception as e:
        logging.error(f"Error during text-to-speech conversion: {e}")


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}!\n\nStart sending messages with me to generate a response.\n\nSend /new to start a new chat session.",
    )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    help_text = """
Basic commands:
/start - Start the bot
/help - Get help. Shows this message

Chat commands:
/new - Start a new chat session (model will forget previously generated messages)
/model - Change the model used to generate responses.
/system_prompt - Change the system prompt used for new chat sessions.
/info - Get info about the current chat session.
/audio - Enable/disable voice synthesis for responses.

Send a message to the bot to generate a response.
"""
    await update.message.reply_text(help_text)


async def new_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start a new chat session."""
    new_chat(context)
    await update.message.reply_text(
        "New chat session started.\n\nSwitch models with /model."
    )


async def model_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Change the model used to generate responses."""
    models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama-3.1-70b-versatile", "gemma2-9b-it", "llama3-8b-8192", "llama3-70b-8192", "mixtral-8x7b-32768", "gemma-7b-it"]

    reply_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(model, callback_data="change_model_" + model)]
            for model in models
        ]
    )

    await update.message.reply_text("Select a model:", reply_markup=reply_markup)


async def change_model_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Change the model used to generate responses."""
    query = update.callback_query
    model = query.data.replace("change_model_", "")

    context.user_data["model"] = model

    await query.edit_message_text(
        f"Model changed to `{model}`. \n\nSend /new to start a new chat session.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def start_system_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start a system prompt."""
    await update.message.reply_text(
        "Send me a system prompt. If you want to clear the system prompt, send `clear` now."
    )
    return SYSTEM_PROMPT_SP


async def cancelled_system_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancel the system prompt."""
    await update.message.reply_text("System prompt change cancelled.")
    return ConversationHandler.END


async def get_system_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get the system prompt."""
    system_prompt = update.message.text
    if system_prompt.lower().strip() == "clear":
        context.user_data.pop("system_prompt", None)
        await update.message.reply_text("System prompt cleared.")
    else:
        context.user_data["system_prompt"] = system_prompt
        await update.message.reply_text("System prompt changed.")
    new_chat(context)
    return ConversationHandler.END


async def audio_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the text-to-speech (TTS) functionality."""
    current_state = context.user_data.get("voice_enabled", True)
    new_state = not current_state
    context.user_data["voice_enabled"] = new_state

    state_text = "enabled" if new_state else "disabled"
    await update.message.reply_text(f"Voice synthesis has been {state_text}.")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages, including TTS conversion."""
    if "model" not in context.user_data:
        context.user_data["model"] = "llama-3.1-8b-instant"

    if "messages" not in context.user_data:
        context.user_data["messages"] = []

    init_msg = await update.message.reply_text("Generating response...")

    message = update.message.text
    if not message:
        return

    asyncio.run_coroutine_threadsafe(update.message.chat.send_action(ChatAction.TYPING), loop=asyncio.get_event_loop())
    full_output_message = ""

    try:
        for message_part in generate_response(message, context):
            if message_part:
                full_output_message += message_part
                send_message = format_message(full_output_message)
                init_msg = await init_msg.edit_text(
                    send_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )

        context.user_data["messages"] = context.user_data.get("messages", []) + [
            {
                "role": "assistant",
                "content": full_output_message,
            }
        ]

        # Generate and send voice response if TTS is enabled
        voice_enabled = context.user_data.get("voice_enabled", True)
        if voice_enabled:
            try:
                audio_buffer = BytesIO()
                text_to_wav(full_output_message, "temp_audio.wav")

                with open("temp_audio.wav", "rb") as wav_file:
                    audio_buffer.write(wav_file.read())
                audio_buffer.seek(0)

                await update.message.reply_voice(voice=audio_buffer)
            except Exception as e:
                logging.exception(f"Error generating or sending audio: {e}")

    except Exception as e:
        logging.exception(f"Error processing the request: {e}")
        await init_msg.edit_text("An error occurred while processing your request.")


async def info_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Get info about the bot."""
    message = f"""**__Conversation Info:__**
**Model**: `{context.user_data.get("model", "llama3-8b-8192")}`
"""
    await update.message.reply_text(format_message(message), parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logging.getLogger(__name__).error("Exception while handling an update:", exc_info=context.error)

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        "An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    await update.message.reply_text(
        text=message, parse_mode=ParseMode.HTML
    )  # You may want to notify admins


def start_bot():
    logger.info("Starting bot")

    app_builder = Application.builder().token(os.getenv("BOT_TOKEN"))

    # Add persistence if available
    if persistence:
        app_builder.persistence(persistence)

    # Build the app
    app = app_builder.build()

    app.add_handler(CommandHandler("start", start, filters=AuthFilter))
    app.add_handler(CommandHandler("help", help_command, filters=AuthFilter))
    app.add_handler(CommandHandler("new", new_command_handler, filters=AuthFilter))
    app.add_handler(CommandHandler("model", model_command_handler, filters=AuthFilter))
    app.add_handler(CommandHandler("info", info_command_handler, filters=AuthFilter))
    app.add_handler(CommandHandler("audio", audio_command_handler, filters=AuthFilter))  # New command

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("system_prompt", start_system_prompt, filters=AuthFilter)
            ],
            states={
                SYSTEM_PROMPT_SP: [MessageHandler(MessageFilter, get_system_prompt)],
                CANCEL_SP: [
                    CommandHandler(
                        "cancel", cancelled_system_prompt, filters=AuthFilter
                    )
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancelled_system_prompt, filters=AuthFilter)
            ],
        )
    )

    app.add_handler(MessageHandler(MessageFilter, message_handler))
    app.add_handler(
        CallbackQueryHandler(change_model_callback_handler, pattern="^change_model_")
    )

    app.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    start_bot()

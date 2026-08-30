import os
import re
import asyncio
import random
import logging
from urllib.parse import urlparse
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Load environment variables
load_dotenv()
BOT_TOKEN = "8800383991:AAHiF7XeKYD3anPw3_QVPq8qNFQtI6LJQEY"

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_URL = 1
WAITING_FOR_N = 2

def extract_whatsapp_number(final_url: str) -> str | None:
    """Extracts a validated WhatsApp number from the final redirected URL."""
    number = None
    
    # Check for wa.me format
    wa_me_match = re.search(r"wa\.me/([^/?]+)", final_url)
    if wa_me_match:
        number = wa_me_match.group(1)
    else:
        # Check for api.whatsapp.com format
        api_match = re.search(r"api\.whatsapp\.com/send\?phone=([^&]+)", final_url)
        if api_match:
            number = api_match.group(1)

    if number:
        # Strip all non-digit characters (keep digits)
        clean_number = re.sub(r"\D", "", number)
        # Validate length (most international numbers with country codes are 10-15 digits)
        if len(clean_number) >= 10:
            return clean_number
            
    return None

async def visit_and_get_number(url: str, browser) -> str | None:
    """Visits the URL using an isolated Playwright context and extracts the number."""
    # Create a completely fresh, isolated browser context (clears cookies/local storage)
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720}
    )
    
    page = await context.new_page()
    
    # Block unnecessary resources (images, css) to speed up loading
    await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

    extracted_number = None
    try:
        # Go to target URL
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        
        # Wait for redirection to WhatsApp domain (max 15 seconds)
        pattern = re.compile(r"(wa\.me|api\.whatsapp\.com)")
        try:
            await page.wait_for_url(pattern, timeout=15000)
        except PlaywrightTimeoutError:
            # Sometimes the redirect happens but doesn't trigger the url event properly, check current URL
            pass 
        
        final_url = page.url
        extracted_number = extract_whatsapp_number(final_url)
        
    except Exception as e:
        logger.error(f"Error during visit: {e}")
    finally:
        await context.close()
        
    return extracted_number

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send welcome message and inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("🚀 Start Collection", callback_data="start_collection"),
            InlineKeyboardButton("❓ Help", callback_data="help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "Welcome to the WA Number Extractor Bot! 🤖\n\n"
        "I can visit redirector URLs, bypass JavaScript loading screens, "
        "and extract WhatsApp numbers for you."
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_text, reply_markup=reply_markup)
        
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "start_collection":
        await query.edit_message_text(
            "Please send me the target URL.\n"
            "(Reply with 'test' to run a mock test without hitting a real site)"
        )
        return WAITING_FOR_URL
    elif query.data == "help":
        help_text = (
            "**How to use:**\n"
            "1. Click 'Start Collection'\n"
            "2. Send the target URL (must start with http/https)\n"
            "3. Send the number of visits (e.g., 20)\n\n"
            "The bot will spin up a headless browser, isolate cookies for every request, "
            "wait for JS redirects, and collect unique WhatsApp numbers."
        )
        await query.edit_message_text(help_text, parse_mode='Markdown')
        return ConversationHandler.END

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the URL and ask for the number of visits."""
    url = update.message.text.strip()
    
    # Handle test mode
    if url.lower() == "test":
        context.user_data['target_url'] = "test"
        await update.message.reply_text("Test mode enabled. How many mock visits should I perform? (Enter a number)")
        return WAITING_FOR_N

    # Validate URL
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        await update.message.reply_text("❌ Invalid URL format. Please send a valid URL starting with http:// or https://")
        return WAITING_FOR_URL
        
    context.user_data['target_url'] = url
    await update.message.reply_text(f"URL saved: {url}\n\nHow many times should I visit this link? (Enter a number, e.g., 20)")
    return WAITING_FOR_N

async def receive_n(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store N, process the visits asynchronously, and return results."""
    n_str = update.message.text.strip()
    
    if not n_str.isdigit() or int(n_str) <= 0:
        await update.message.reply_text("❌ Please enter a valid positive number.")
        return WAITING_FOR_N
        
    n_visits = int(n_str)
    url = context.user_data.get('target_url')
    
    await update.message.reply_text(f"🔄 Processing... I will visit the URL {n_visits} times. Please wait, this might take a few minutes depending on the redirect delays.")
    
    # Process asynchronously to not block the bot
    asyncio.create_task(process_visits(update.effective_chat.id, url, n_visits, context.bot))
    return ConversationHandler.END

async def process_visits(chat_id: int, url: str, n_visits: int, bot):
    """Background task to run Playwright and collect numbers."""
    unique_numbers = set()
    successful_visits = 0
    
    if url == "test":
        # Mock logic for testing
        for i in range(n_visits):
            await asyncio.sleep(0.5) # simulate delay
            mock_numbers = ["919876543210", "919876543211", "919876543212"]
            unique_numbers.add(random.choice(mock_numbers))
            successful_visits += 1
    else:
        # Real logic using Playwright
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                
                for i in range(n_visits):
                    logger.info(f"Visit {i+1}/{n_visits} for URL: {url}")
                    
                    number = await visit_and_get_number(url, browser)
                    if number:
                        unique_numbers.add(number)
                        successful_visits += 1
                        
                    # Random delay between requests to mimic human behavior
                    if i < n_visits - 1:
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        
                await browser.close()
        except Exception as e:
            logger.error(f"Playwright initialization error: {e}")
            await bot.send_message(chat_id=chat_id, text="⚠️ Critical Error: Failed to launch browser. Make sure `playwright install` was run on the server.")
            return

    # Format output
    if not unique_numbers:
        await bot.send_message(chat_id=chat_id, text=f"✅ Completed {n_visits} visits.\n❌ Could not extract any WhatsApp numbers. The site might be blocking headless browsers or timing out.")
        return

    result_text = (
        f"📊 **Scraping Complete**\n\n"
        f"• Total visits attempted: {n_visits}\n"
        f"• Successful extractions: {successful_visits}\n"
        f"• Unique numbers found: **{len(unique_numbers)}**\n\n"
        f"**Extracted Links:**\n"
    )
    
    for num in unique_numbers:
        result_text += f"• https://wa.me/{num}\n"
        
    # Telegram messages have a 4096 char limit, chunk if necessary
    if len(result_text) > 4000:
        result_text = result_text[:4000] + "\n... (Truncated due to length)"
        
    await bot.send_message(chat_id=chat_id, text=result_text, disable_web_page_preview=True, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text("Operation cancelled. Type /start to begin again.")
    return ConversationHandler.END

def main() -> None:
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not found in environment variables.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Set up conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)],
            WAITING_FOR_N: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_n)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

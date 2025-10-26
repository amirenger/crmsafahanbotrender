import os
import logging
import sqlite3
from datetime import datetime
import json

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
)
from google import genai
from google.genai.errors import APIError

# --- تنظیمات لاگ‌گیری (Logging) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# =================================================================
# --- متغیرهای حیاتی و محیطی (Environment Variables) ---
# در Render، این مقادیر از طریق متغیرهای محیطی (OS) تنظیم می شوند
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_API_KEY_HERE")
DB_FILE = "crm_free_form_data.db" 

# متغیرهای Webhook/Render
PORT = int(os.environ.get('PORT', '8000')) 
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL") 
# =================================================================

# --- آماده‌سازی هوش مصنوعی (AI Setup) و ثابت‌ها ---
ai_client = None
AI_MODEL = 'gemini-2.5-flash'
TODAY_DATE = datetime.now().strftime("%Y-%m-%d")

if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_API_KEY_HERE":
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini Client and Model Initialized Successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
else:
    logger.warning("GEMINI_API_KEY not set. AI client initialization skipped.")


# --- توابع دیتابیس (SQLite) ---

def init_db():
    """ایجاد یا اتصال به دیتابیس و ساخت جداول مورد نیاز."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            company TEXT,
            industry TEXT,
            services TEXT,
            crm_user_id INTEGER NOT NULL,
            UNIQUE(name, phone)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            interaction_date TEXT NOT NULL,
            report TEXT NOT NULL,
            follow_up_date TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Free-Form CRM Database {DB_FILE} initialized.")

# =================================================================
# --- توابع (Functions) که هوش مصنوعی به آنها دسترسی دارد (Tools) ---
# =================================================================

def add_new_customer(name: str, phone: str, company: str = None, industry: str = None, services: str = None) -> str:
    """
    برای ثبت یک مشتری جدید در دیتابیس استفاده می شود. نام و شماره تلفن الزامی هستند. 
    اگر مشتری با این نام و شماره قبلا وجود داشته باشد، خطا بازگردانده می شود.
    Gemini باید قبل از فراخوانی مطمئن شود که نام و تلفن در دسترس است.
    """
    if not name or not phone:
        return "خطا: نام و شماره تلفن برای ثبت مشتری جدید الزامی هستند."
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    crm_user_id = 0 

    try:
        # جایگزینی مقادیر Null با None برای دیتابیس
        company = company if company else None
        industry = industry if industry else None
        services = services if services else None
        
        cursor.execute("""
            INSERT INTO customers (name, phone, company, industry, services, crm_user_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, phone, company, industry, services, crm_user_id))
        conn.commit()
        customer_id = cursor.lastrowid
        return f"عملیات ثبت مشتری موفق بود. مشتری '{name}' (ID: {customer_id}) با موفقیت ثبت شد."
    except sqlite3.IntegrityError:
        return f"خطا: مشتری با نام '{name}' و شماره '{phone}' قبلا در سیستم ثبت شده است."
    finally:
        conn.close()


def log_interaction(customer_name: str, interaction_report: str, follow_up_date: str = None) -> str:
    """
    برای ثبت گزارش تماس یا تعامل جدید با یک مشتری موجود استفاده می شود.
    باید ابتدا مشتری را با 'customer_name' پیدا کند.
    اگر تاریخ پیگیری به صورت 'هفته آینده' یا 'ماه بعد' باشد، Gemini باید آن را به فرمت YYYY-MM-DD تبدیل کند.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM customers WHERE name = ? COLLATE NOCASE", (customer_name,))
    result = cursor.fetchone()
    
    if not result:
        conn.close()
        return f"خطا: مشتری با نام '{customer_name}' در دیتابیس پیدا نشد. لطفا ابتدا او را ثبت کنید."

    customer_id = result[0]
    
    cursor.execute("""
        INSERT INTO interactions (customer_id, interaction_date, report, follow_up_date)
        VALUES (?, ?, ?, ?)
    """, (customer_id, TODAY_DATE, interaction_report, follow_up_date))
    conn.commit()
    conn.close()
    
    follow_up_msg = f"پیگیری بعدی برای تاریخ {follow_up_date} تنظیم شد." if follow_up_date else ""
    return f"گزارش تماس با '{customer_name}' با موفقیت ثبت شد. {follow_up_msg}"


def get_customer_info(name_or_industry: str, info_type: str = "full_report") -> str:
    """
    برای دریافت گزارش یا اطلاعات خاصی از مشتری یا گروهی از مشتریان استفاده می شود.
    پارامتر info_type می تواند: 'full_report' (گزارش کامل یک مشتری), 'chance_analysis' (تحلیل شانس فروش) یا 'industry_list' (لیست مشتریان یک حوزه کاری) باشد.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    output = []

    if info_type == 'full_report':
        cursor.execute("SELECT * FROM customers WHERE name = ? COLLATE NOCASE", (name_or_industry,))
        customer = cursor.fetchone()
        
        if not customer:
            return f"خطا: مشتری با نام '{name_or_industry}' پیدا نشد."
            
        keys = ["ID", "نام", "تلفن", "شرکت", "حوزه کاری", "خدمات مورد نظر", "CRM User ID"]
        output.append("جزئیات مشتری:\n" + json.dumps(dict(zip(keys, customer)), ensure_ascii=False, indent=2))

        cursor.execute("SELECT interaction_date, report, follow_up_date FROM interactions WHERE customer_id = ?", (customer[0],))
        interactions = cursor.fetchall()
        
        if interactions:
            output.append("\nگزارشات تعامل:\n")
            for date, report, follow_up in interactions:
                output.append(f"  - تاریخ: {date}, پیگیری: {follow_up or 'ندارد'}\n    خلاصه: {report[:100]}...")
        else:
            output.append("هیچ گزارش تعاملی ثبت نشده است.")
        
        return "\n".join(output)
        
    elif info_type == 'industry_list':
        cursor.execute("SELECT name, phone, company FROM customers WHERE industry LIKE ?", ('%' + name_or_industry + '%',))
        customers = cursor.fetchall()
        
        if not customers:
            return f"هیچ مشتری در حوزه '{name_or_industry}' پیدا نشد."
            
        output.append(f"مشتریان در حوزه '{name_or_industry}':\n")
        for name, phone, company in customers:
            output.append(f"- {name} ({company or '---'}): {phone}")
        return "\n".join(output)

    elif info_type == 'chance_analysis':
        cursor.execute("SELECT name FROM customers")
        all_customers = [row[0] for row in cursor.fetchall()]
        
        analysis_data = []
        for name in all_customers:
            cursor.execute("""
                SELECT report 
                FROM interactions i JOIN customers c ON i.customer_id = c.id 
                WHERE c.name = ?
            """, (name,))
            reports = [row[0] for row in cursor.fetchall()]
            analysis_data.append({"customer": name, "reports": reports})
            
        return json.dumps(analysis_data, ensure_ascii=False)
        
    conn.close()
    return "خطای ناشناخته در تابع get_customer_info."

# =================================================================
# --- تابع اصلی هندلر پیام (Free-Form Handler) با قابلیت حافظه ---
# =================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    هندلر واحد برای پردازش تمام پیام‌ها. از Function Calling و حافظه مکالمه استفاده می‌کند.
    """
    
    if not ai_client:
        await update.message.reply_text("❌ سرویس هوش مصنوعی غیرفعال است.")
        return

    user_text = update.message.text
    
    # --- 1. مدیریت حافظه مکالمه (Conversation History) ---
    if 'history' not in context.user_data:
        context.user_data['history'] = []
    
    # [اصلاح نهایی] استفاده مستقیم از متن (String) به عنوان Part برای جلوگیری از TypeError
    user_part = user_text 
    
    # افزودن پیام جدید کاربر به تاریخچه
    context.user_data['history'].append(genai.types.Content(role="user", parts=[user_part]))
    
    conversation_history = context.user_data['history']
    
    # تعریف پرامپت سیستمی (System Instruction)
    system_instruction = (
        "شما یک دستیار هوشمند CRM با **حافظه کامل مکالمه** هستید. وظیفه اصلی شما ثبت دقیق داده ها، گزارش تماس ها، و پاسخگویی تحلیلی است. "
        "**اولویت شما انجام عملیات با حداقل داده‌های اجباری (نام و تلفن برای ثبت) است**، حتی اگر در مراحل قبلی داده‌هایی مثل شرکت یا خدمات را درخواست کرده‌اید. "
        "اگر کاربر داده‌ها را در چند پیام متوالی ارائه کرد، باید اطلاعات را از **تاریخچه مکالمه** استخراج و عملیات را تکمیل کنید. "
        "شما به توابع دیتابیس (add_new_customer, log_interaction, get_customer_info) دسترسی دارید. "
        "**قوانین:** 1. هرگاه داده‌های اجباری برای یک تابع (مثل نام و تلفن برای add_new_customer) جمع‌آوری شد، فوراً آن تابع را فراخوانی کنید. 2. همیشه پاسخ های خود را به زبان فارسی و دوستانه بنویسید."
    )

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='TYPING')
    
    try:
        # مرحله ۱: ارسال درخواست با تاریخچه مکالمه
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=conversation_history, 
            config=genai.types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[add_new_customer, log_interaction, get_customer_info]
            )
        )
        
        # --- تحلیل پاسخ هوش مصنوعی ---
        
        # ۱. اگر هوش مصنوعی خواست یک تابع را اجرا کند (Function Call)
        if response.function_calls:
            function_calls = response.function_calls
            tool_responses = []
            
            # ذخیره فراخوانی تابع AI در تاریخچه
            context.user_data['history'].append(genai.types.Content(role="model", parts=[genai.types.Part.from_function_calls(function_calls)]))
            
            for call in function_calls:
                function_name = call.name
                args = dict(call.args)
                
                # اجرای تابع مورد نظر
                if function_name == 'add_new_customer':
                    tool_result = add_new_customer(**args)
                elif function_name == 'log_interaction':
                    tool_result = log_interaction(**args)
                elif function_name == 'get_customer_info':
                    tool_result = get_customer_info(**args)
                else:
                    tool_result = f"خطا: تابع {function_name} ناشناخته است."
                    
                tool_responses.append(
                    genai.types.Part.from_function_response(
                        name=function_name,
                        response={"result": tool_result}
                    )
                )

            # ذخیره نتیجه اجرای توابع در تاریخچه
            context.user_data['history'].append(genai.types.Content(role="tool", parts=tool_responses))
            
            # مرحله ۲: ارسال نتیجه و تاریخچه به‌روز شده به Gemini برای تولید پاسخ نهایی
            final_response = ai_client.models.generate_content(
                model=AI_MODEL,
                contents=context.user_data['history'], 
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[add_new_customer, log_interaction, get_customer_info]
                )
            )
            
            # ذخیره پاسخ نهایی AI در تاریخچه
            context.user_data['history'].append(final_response.candidates[0].content)

            await update.message.reply_text(final_response.text)

        # ۲. اگر هوش مصنوعی مستقیماً پاسخ داد (بدون نیاز به دیتابیس)
        else:
            # ذخیره پاسخ مستقیم AI در تاریخچه
            context.user_data['history'].append(response.candidates[0].content)
            await update.message.reply_text(response.text)

    except APIError as e:
        logger.error(f"Gemini API Error: {e}")
        await update.message.reply_text("⚠️ خطای API رخ داد. لطفاً چند دقیقه دیگر امتحان کنید.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        await update.message.reply_text(f"❓ یک خطای نامشخص رخ داد: {e}. لطفا دوباره تلاش کنید.")


# --- توابع هندلر کمکی (اختیاری) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پاسخ به دستور /start و راهنمایی اولیه."""
    init_db() 
    
    # برای شروع مکالمه جدید، تاریخچه را پاک کنید
    if 'history' in context.user_data:
        del context.user_data['history']
        
    ai_status = "✅ متصل و آماده" if ai_client else "❌ غیرفعال (کلید API را بررسی کنید)."
    
    message = (
        f"🤖 **CRM Bot هوشمند با حافظه کامل (Free-Form)**\n\n"
        f"✨ وضعیت AI: {ai_status}\n"
        f"**نحوه استفاده:** هرگونه پیام یا درخواستی که دارید را ارسال کنید. من پیام‌های شما را به صورت پیوسته به خاطر می‌سپارم و نیت شما را درک می‌کنم.\n\n"
        f"**مثال‌ها:**\n"
        f" - **ثبت:** 'با آقای نوری صحبت کردم. تلفنش ۰۹۱۱۱۰۰۰۰۰۱ بود.'\n"
        f" - **ادامه ثبت:** 'شرکتشون سمنان بتنه و خدمات اینستاگرام بهش دادیم.'\n"
        f" - **گزارش:** 'حالا گزارش کامل نوری رو بهم بده.'\n"
    )
    await update.message.reply_text(message)

# --- تابع اصلی اجرا (Main Execution Function) ---

def main() -> None:
    """شروع به کار ربات (با منطق انتخاب Webhook یا Polling)"""
    init_db() 
    
    # بررسی Webhook (برای Render)
    if RENDER_EXTERNAL_URL and TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        # --- اجرای Webhook ---
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

        url_path = TELEGRAM_BOT_TOKEN 
        webhook_url = f"{RENDER_EXTERNAL_URL}/{url_path}"
        
        logger.info(f"Setting up Webhook at {webhook_url} on port {PORT}")

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=webhook_url
        )
        logger.info("Webhook set up successfully. Bot is running on Render.")
        return

    # --- اجرای Polling (برای تست لوکال) ---
    logger.warning("RENDER_EXTERNAL_URL not set or Token not defined. Running in polling mode (for local testing).")
    
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("TELEGRAM_BOT_TOKEN is a placeholder. Cannot run bot.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Starting Memory-Enabled Free-Form CRM Bot (Polling Mode)...")
    application.run_polling(poll_interval=3.0)
    
if __name__ == "__main__":
    main()
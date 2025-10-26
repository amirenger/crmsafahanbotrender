import os
import logging
import sqlite3
from datetime import datetime
import json
import asyncio # برای قابلیت هشدار در بک‌گراند

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
)
from google import genai
from google.genai import types 
from google.genai.errors import APIError

# --- تنظیمات لاگ‌گیری ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# =================================================================
# --- متغیرهای حیاتی و محیطی ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_API_KEY_HERE")
DB_FILE = "crm_free_form_data.db" 

# متغیرهای Webhook/Render
PORT = int(os.environ.get('PORT', '8000')) 
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL") 
# =================================================================

# --- آماده‌سازی هوش مصنوعی و ثابت‌ها ---
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
    
    # جدول مشتریان (Customer Table)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            company TEXT,
            industry TEXT,
            services TEXT,
            crm_user_id INTEGER DEFAULT 0,
            UNIQUE(name, phone)
        )
    """)
    
    # جدول تعاملات (Interactions Table)
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
    
    # جدول هشدارها (Reminders Table) برای قابلیت ۳
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            customer_name TEXT,
            reminder_text TEXT NOT NULL,
            due_date_time TEXT NOT NULL,
            sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Free-Form CRM Database {DB_FILE} initialized.")

# =================================================================
# --- توابع (Functions) که هوش مصنوعی به آنها دسترسی دارد (Tools) ---
# =================================================================

def manage_customer_data(name: str, phone: str, company: str = None, industry: str = None, services: str = None) -> str:
    """
    برای ثبت مشتری جدید یا به‌روزرسانی اطلاعات مشتری موجود استفاده می‌شود. (قابلیت ۱ و ۲)
    اگر مشتری با نام و تلفن وجود داشته باشد، اطلاعات غیرخالی آن به‌روز می‌شود. نام و تلفن الزامی هستند.
    """
    if not name or not phone:
        return "خطا: نام و شماره تلفن برای ثبت یا به‌روزرسانی مشتری الزامی هستند."
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. جستجوی مشتری موجود
    cursor.execute("SELECT id FROM customers WHERE name = ? AND phone = ?", (name, phone))
    existing_customer = cursor.fetchone()

    if existing_customer:
        customer_id = existing_customer[0]
        updates = []
        params = []
        if company:
            updates.append("company = ?")
            params.append(company)
        if industry:
            updates.append("industry = ?")
            params.append(industry)
        if services:
            updates.append("services = ?")
            params.append(services)
        
        if updates:
            query = f"UPDATE customers SET {', '.join(updates)} WHERE id = ?"
            params.append(customer_id)
            cursor.execute(query, tuple(params))
            conn.commit()
            conn.close()
            return f"اطلاعات مشتری '{name}' (ID: {customer_id}) با موفقیت به‌روزرسانی شد."
        else:
            conn.close()
            return f"مشتری '{name}' (ID: {customer_id}) قبلاً ثبت شده و اطلاعات جدیدی برای به‌روزرسانی وجود نداشت."
    else:
        # 2. ثبت مشتری جدید
        crm_user_id = 0 
        try:
            cursor.execute("""
                INSERT INTO customers (name, phone, company, industry, services, crm_user_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, phone, company, industry, services, crm_user_id))
            conn.commit()
            customer_id = cursor.lastrowid
            conn.close()
            return f"عملیات ثبت مشتری موفق بود. مشتری '{name}' (ID: {customer_id}) با موفقیت ثبت شد."
        except sqlite3.IntegrityError:
            conn.close()
            return f"خطا: مشتری با نام '{name}' و شماره '{phone}' قبلا در سیستم ثبت شده است."
        except Exception as e:
            conn.close()
            return f"خطای ناشناخته در ثبت مشتری: {e}"


def log_interaction(customer_name: str, interaction_report: str, follow_up_date: str = None) -> str:
    """
    برای ثبت گزارش تماس یا تعامل جدید با یک مشتری موجود استفاده می شود. (قابلیت ۲)
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


def set_reminder(customer_name: str, reminder_text: str, date_time: str, chat_id: int) -> str:
    """
    برای تنظیم یک یادآوری یا هشدار در مورد مشتری یا هر رویداد دیگری استفاده می شود. (قابلیت ۳)
    تاریخ و زمان باید به فرمت دقیق 'YYYY-MM-DD HH:MM:SS' یا 'YYYY-MM-DD' توسط Gemini تبدیل شوند.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO reminders (chat_id, customer_name, reminder_text, due_date_time)
            VALUES (?, ?, ?, ?)
        """, (chat_id, customer_name, reminder_text, date_time))
        conn.commit()
        return f"هشدار با متن '{reminder_text[:30]}...' برای {date_time} با موفقیت ثبت شد."
    except Exception as e:
        return f"خطا در ثبت هشدار: {e}"
    finally:
        conn.close()


def get_report(query_type: str, search_term: str = None, fields: str = "all") -> str:
    """
    برای دریافت گزارش یا اطلاعات خاصی از مشتریان (مانند گزارش هوشمند صنفی) استفاده می شود. (قابلیت ۴)
    query_type می تواند: 'full_customer' (گزارش کامل یک مشتری), 'industry_search' (جستجوی مشتریان یک صنف), یا 'interaction_summary' (خلاصه تعاملات).
    fields یک رشته است که فیلدهای مورد نیاز را با کاما جدا می کند (مثلاً 'name,phone,company').
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    output = []
    
    if query_type == 'industry_search' and search_term:
        field_names = [f.strip() for f in fields.split(',')]
        
        # اگر فیلدهای خاصی خواسته نشده، فقط نام و تلفن را بگیرید
        select_fields = ", ".join(field_names) if fields != "all" else "name, phone, company, industry"
        
        cursor.execute(f"SELECT {select_fields} FROM customers WHERE industry LIKE ?", ('%' + search_term + '%',))
        customers = cursor.fetchall()
        
        if not customers:
            return f"هیچ مشتری در حوزه '{search_term}' پیدا نشد."
            
        output.append(f"مشتریان در حوزه '{search_term}' (فیلدهای: {select_fields}):\n")
        # اضافه کردن هدر جدول برای خوانایی
        if fields == "all":
             output.append(" | ".join(["نام", "تلفن", "شرکت", "صنعت"]))
             output.append("-" * 50)
        
        for row in customers:
            output.append(" | ".join([str(item) for item in row]))
            
        conn.close()
        return "\n".join(output)
        
    elif query_type == 'full_customer' and search_term:
        # منطق گزارش کامل مشتری (مثل نسخه قبلی)
        cursor.execute("SELECT * FROM customers WHERE name = ? COLLATE NOCASE", (search_term,))
        customer = cursor.fetchone()
        
        if not customer:
            conn.close()
            return f"خطا: مشتری با نام '{search_term}' پیدا نشد."
            
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
        
        conn.close()
        return "\n".join(output)
        
    conn.close()
    return f"نوع گزارش '{query_type}' پشتیبانی نمی‌شود یا عبارت جستجو مشخص نیست."

# =================================================================
# --- توابع مدیریت تلگرام (قابلیت ۵ و ۶) ---
# =================================================================

async def export_data_to_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    تولید فایل CSV از اطلاعات کامل مشتریان و ارسال آن به کاربر (قابلیت ۵).
    این تابع مستقیماً توسط دکمه تلگرام فراخوانی می‌شود و به Function Calling ربطی ندارد.
    """
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    
    if not customers:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ دیتابیس مشتریان خالی است. فایلی برای ارسال وجود ندارد.")
        conn.close()
        return

    # تولید محتوای CSV
    csv_content = ["ID,نام,تلفن,شرکت,حوزه کاری,خدمات مورد نظر,CRM User ID"]
    for row in customers:
        # جایگزینی کاما با نقطه ویرگول یا حذف آن برای جلوگیری از بهم ریختگی CSV
        safe_row = [str(item).replace(',', ';') if item else '' for item in row]
        csv_content.append(",".join(safe_row))
        
    file_name = f"CRM_Customers_Export_{TODAY_DATE}.csv"
    
    # ارسال فایل (از طریق حافظه در محیط Render)
    await context.bot.send_document(
        chat_id=chat_id, 
        document=bytes("\n".join(csv_content).encode('utf-8')),
        filename=file_name,
        caption="فایل کامل مشتریان CRM با فرمت CSV"
    )
    conn.close()


# =================================================================
# --- وظیفه بک‌گراند برای هشدارها (قابلیت ۳) ---
# =================================================================

async def reminder_checker(application: Application):
    """وظیفه دوره‌ای برای بررسی و ارسال هشدارهای ثبت شده."""
    while True:
        await asyncio.sleep(60) # هر ۶۰ ثانیه یک بار چک می‌کند
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # جستجوی هشدارهایی که زمان آنها رسیده و هنوز ارسال نشده‌اند
        # استفاده از LIKE برای تطبیق با تاریخ کامل یا فقط تاریخ
        cursor.execute("""
            SELECT id, chat_id, customer_name, reminder_text 
            FROM reminders 
            WHERE due_date_time LIKE ? || '%' AND sent = 0
        """, (current_time_str[:16],)) # تطبیق تا دقیقه
        
        reminders = cursor.fetchall()
        
        for reminder_id, chat_id, customer_name, reminder_text in reminders:
            try:
                # ارسال پیام هشدار
                message = f"🔔 **هشدار CRM**\n\nمشتری: **{customer_name or 'عمومی'}**\nپیام: _{reminder_text}_\n\n"
                await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                
                # به‌روزرسانی وضعیت ارسال
                cursor.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to send reminder {reminder_id} to {chat_id}: {e}")
                
        conn.close()

# =================================================================
# --- تابع اصلی هندلر پیام (Free-Form Handler) ---
# =================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هندلر اصلی برای پردازش پیام‌ها، Function Calling و تحلیل هوشمند (قابلیت ۷)."""
    
    if not ai_client or not update.message or not update.message.text:
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    # بررسی دکمه‌های آماده (قابلیت ۶)
    if user_text.strip() == "📥 ارسال فایل کل مشتریان":
        await export_data_to_file(update, context)
        return
    
    # برای سایر دکمه‌ها، صرفاً متن را به هوش مصنوعی می‌فرستیم تا تصمیم بگیرد (مثلاً برای "ثبت اطلاعات جدید")
    
    # --- 1. مدیریت حافظه مکالمه (Conversation History) ---
    if 'history' not in context.user_data:
        context.user_data['history'] = []
    
    # [اصلاح قطعی] ساخت آبجکت Part
    user_part = types.Part(text=user_text)
    
    # افزودن پیام جدید کاربر به تاریخچه
    context.user_data['history'].append(types.Content(role="user", parts=[user_part]))
    
    conversation_history = context.user_data['history']
    
    # تعریف پرامپت سیستمی (System Instruction) (قابلیت ۷)
    system_instruction = (
        "شما یک دستیار هوشمند CRM با **حافظه کامل و تحلیلگر هوشمند** هستید. "
        "وظایف شما: ۱. ثبت و به‌روزرسانی دقیق داده‌ها، ثبت گزارش‌ها و تنظیم هشدارها با استفاده از توابع (Tools). "
        "۲. ارائه گزارش هوشمند و فیلتر شده (قابلیت ۴). "
        "۳. **تحلیل هوشمند و ارائه پیشنهاد عملی (قابلیت ۷):** پس از اجرای موفقیت‌آمیز هر تابع **ثبت**، باید داده‌های جدید و تاریخچه را تحلیل کنید و **به صورت یک پاراگراف جداگانه**، یک پیشنهاد عملی (Actionable Advice) برای پیگیری بعدی یا بهبود روند فروش ارائه دهید (مانند بهترین زمان تماس، پیشنهادات رقابتی، یا مراحل بعدی). "
        "**قوانین:** 1. هرگاه داده‌های اجباری برای یک تابع جمع‌آوری شد، آن را فراخوانی کنید. 2. همیشه پاسخ های خود را به زبان فارسی و دوستانه بنویسید. 3. در فراخوانی تابع set_reminder، 'chat_id' را برابر با **" + str(chat_id) + "** قرار دهید."
    )

    await context.bot.send_chat_action(chat_id=chat_id, action='TYPING')
    
    try:
        # مرحله ۱: ارسال درخواست با تاریخچه مکالمه
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=conversation_history, 
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[manage_customer_data, log_interaction, set_reminder, get_report] # لیست توابع جدید
            )
        )
        
        # --- تحلیل پاسخ هوش مصنوعی ---
        if response.function_calls:
            function_calls = response.function_calls
            tool_responses = []
            
            context.user_data['history'].append(types.Content(role="model", parts=[types.Part.from_function_calls(function_calls)]))
            
            for call in function_calls:
                function_name = call.name
                args = dict(call.args)
                
                # اجرای تابع مورد نظر
                if function_name == 'manage_customer_data':
                    tool_result = manage_customer_data(**args)
                elif function_name == 'log_interaction':
                    tool_result = log_interaction(**args)
                elif function_name == 'set_reminder':
                    # تزریق chat_id به آرگومان‌های تابع
                    if 'chat_id' not in args: args['chat_id'] = chat_id 
                    tool_result = set_reminder(**args)
                elif function_name == 'get_report':
                    tool_result = get_report(**args)
                else:
                    tool_result = f"خطا: تابع {function_name} ناشناخته است."
                    
                tool_responses.append(
                    types.Part.from_function_response(
                        name=function_name,
                        response={"result": tool_result}
                    )
                )

            context.user_data['history'].append(types.Content(role="tool", parts=tool_responses))
            
            # مرحله ۲: ارسال نتیجه به Gemini برای تولید پاسخ نهایی (شامل تحلیل هوشمند)
            final_response = ai_client.models.generate_content(
                model=AI_MODEL,
                contents=context.user_data['history'], 
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[manage_customer_data, log_interaction, set_reminder, get_report]
                )
            )
            
            if final_response.candidates and final_response.candidates[0].content:
                context.user_data['history'].append(final_response.candidates[0].content)

            await update.message.reply_text(final_response.text, parse_mode='Markdown')

        else:
            # ذخیره پاسخ مستقیم AI در تاریخچه
            if response.candidates and response.candidates[0].content:
                context.user_data['history'].append(response.candidates[0].content)
            await update.message.reply_text(response.text, parse_mode='Markdown')

    except APIError as e:
        logger.error(f"Gemini API Error: {e}")
        await update.message.reply_text("⚠️ خطای API رخ داد. لطفاً چند دقیقه دیگر امتحان کنید.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        await update.message.reply_text(f"❓ یک خطای نامشخص رخ داد. لطفاً لاگ‌های سرور را بررسی کنید. خطا: {e}")


# --- توابع هندلر کمکی ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پاسخ به دستور /start و راهنمایی اولیه."""
    init_db() 
    
    if 'history' in context.user_data:
        del context.user_data['history']
        
    ai_status = "✅ متصل و آماده" if ai_client else "❌ غیرفعال (کلید API را بررسی کنید)."
    
    # تعریف دکمه‌های ریپلای برای سهولت کار (قابلیت ۶)
    reply_keyboard = [
        ["✍️ ثبت اطلاعات جدید", "📞 ثبت گزارش تماس"],
        ["📊 درخواست گزارش هوشمند", "📥 ارسال فایل کل مشتریان"],
    ]
    markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=False, resize_keyboard=True)
    
    message = (
        f"🤖 **CRM Bot هوشمند با تحلیل و حافظه کامل**\n\n"
        f"✨ وضعیت AI: {ai_status}\n"
        f"**نحوه استفاده:** هرگونه پیام یا درخواستی که دارید را ارسال کنید، یا از دکمه‌های زیر استفاده کنید. ربات نیت شما را درک و عملیات لازم را انجام می‌دهد و **پیشنهاد هوشمندانه** می‌دهد.\n\n"
        f"**مثال‌های هوشمند:**\n"
        f" - **ثبت و تحلیل:** 'با آقای نوری صحبت کردم. گفت قیمت رقبا بالاتره.'\n"
        ff" - **هشدار:** 'برای هفته بعد دوشنبه ساعت ۱۰ صبح پیگیری با نوری رو برام یادآوری کن.'\n"
    )
    
    await update.message.reply_text(message, reply_markup=markup, parse_mode='Markdown')

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
        
        # اجرای وظیفه بک‌گراند هشدار
        application.job_queue.run_once(
            lambda context: asyncio.create_task(reminder_checker(application)),
            0
        )

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
    
    # اجرای وظیفه بک‌گراند هشدار (با استفاده از thread در Polling)
    application.job_queue.run_once(
        lambda context: asyncio.create_task(reminder_checker(application)),
        0
    )

    logger.info("Starting Memory-Enabled Free-Form CRM Bot (Polling Mode)...")
    application.run_polling(poll_interval=3.0)
    
if __name__ == "__main__":
    main()
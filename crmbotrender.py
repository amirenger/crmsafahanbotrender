import os
import logging
from datetime import datetime
import json
import asyncio
import psycopg2 
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ChatAction
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
DATABASE_URL = os.environ.get("DATABASE_URL") # آدرس اتصال PostgreSQL از Render

# متغیرهای Webhook/Render
PORT = int(os.environ.get('PORT', '8000'))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
# =================================================================

# --- آماده‌سازی هوش مصنوعی و ثابت‌ها ---
ai_client = None
AI_MODEL = 'gemini-2.5-flash'
TODAY_DATE = datetime.now().strftime("%Y-%m-%d")

# --- آماده‌سازی دیتابیس PostgreSQL ---
db_connection = None

def get_db_connection():
    """اتصال به PostgreSQL با استفاده از DATABASE_URL."""
    global db_connection
    if db_connection is None or db_connection.closed != 0:
        if not DATABASE_URL:
            logger.error("DATABASE_URL is not set. Persistent memory is disabled.")
            return None
        try:
            # اتصال به دیتابیس PostgreSQL
            # برای اتصال مطمئن در Render، پارامتر sslmode='require' را اضافه می کنیم.
            db_connection = psycopg2.connect(DATABASE_URL, sslmode='require')
            db_connection.autocommit = True
            logger.info("PostgreSQL Connection Established Successfully.")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            db_connection = None
    return db_connection

def init_db():
    """ایجاد جداول در دیتابیس PostgreSQL در صورت عدم وجود."""
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cursor:
            # ۱. جدول مشتریان
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) UNIQUE,
                    company VARCHAR(255),
                    industry VARCHAR(255),
                    services TEXT
                );
            """)
            # ۲. جدول تعاملات
            # REFERENCES customers(name) حذف شد تا در صورت حذف مشتری، گزارشات باقی بماند (بهتر است از customer_id استفاده شود اما برای سادگی فعلی، نام را حذف کردیم)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(255) NOT NULL, 
                    interaction_date DATE,
                    report TEXT,
                    follow_up_date DATE
                );
            """)
            # ۳. جدول هشدارها
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    customer_name VARCHAR(255),
                    reminder_text TEXT,
                    due_date_time TIMESTAMP,
                    sent BOOLEAN DEFAULT FALSE
                );
            """)
            logger.info("PostgreSQL Tables Initialized Successfully. Persistent memory is now ON.")
            return True
    except Exception as e:
        logger.error(f"Error initializing PostgreSQL tables: {e}")
        return False

# --- توابع (Functions) که هوش مصنوعی به آنها دسترسی دارد (Tools) ---

def find_customer_data(name: str, phone: str = None):
    """جستجوی مشتری بر اساس نام و/یا تلفن و بازگرداندن داده‌ها."""
    conn = get_db_connection()
    if conn is None: return None
    try:
        with conn.cursor() as cursor:
            # ابتدا با نام و تلفن جستجو
            if phone:
                cursor.execute("SELECT * FROM customers WHERE name ILIKE %s AND phone = %s", (name, phone))
                result = cursor.fetchone()
                if result: return result
            
            # در غیر این صورت، فقط با نام جستجو
            cursor.execute("SELECT * FROM customers WHERE name ILIKE %s", (name,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Error finding customer: {e}")
        return None
        
def delete_customer(name: str, phone: str = None) -> str:
    """حذف یک مشتری و گزارشات تعامل مرتبط با او. (قابلیت جدید: حذف)"""
    conn = get_db_connection()
    if conn is None:
        return "خطا: سرویس حافظه دائمی (PostgreSQL) فعال نیست."

    customer = find_customer_data(name, phone)
    
    if not customer:
        return f"خطا: مشتری با نام '{name}' در دیتابیس پیدا نشد."
        
    try:
        with conn.cursor() as cursor:
            customer_name = customer[1] 
            customer_id = customer[0]

            # ۱. حذف تعاملات مرتبط (اختیاری)
            cursor.execute("DELETE FROM interactions WHERE customer_name = %s", (customer_name,))
            deleted_interactions = cursor.rowcount
            
            # ۲. حذف یادآوری‌های مرتبط (اختیاری)
            cursor.execute("DELETE FROM reminders WHERE customer_name = %s", (customer_name,))
            deleted_reminders = cursor.rowcount

            # ۳. حذف مشتری اصلی
            cursor.execute("DELETE FROM customers WHERE id = %s", (customer_id,))

            return f"مشتری '{customer_name}' با موفقیت حذف شد. ({deleted_interactions} گزارش تعامل و {deleted_reminders} یادآوری نیز حذف شدند.)"
    except Exception as e:
        return f"خطای دیتابیس در حذف مشتری: {e}"


def manage_customer_data(name: str, phone: str, company: str = None, industry: str = None, services: str = None) -> str:
    """ثبت مشتری جدید یا به‌روزرسانی اطلاعات مشتری موجود. (قابلیت ۱)"""
    conn = get_db_connection()
    if conn is None:
        return "خطا: سرویس حافظه دائمی (PostgreSQL) فعال نیست."
    if not name or not phone:
        return "خطا: نام و شماره تلفن برای ثبت یا به‌روزرسانی مشتری الزامی هستند."

    existing = find_customer_data(name, phone)
    
    try:
        with conn.cursor() as cursor:
            if existing:
                # به‌روزرسانی مشتری موجود
                updates = []
                params = []
                
                # مقایسه و افزودن فیلدهای جدید برای به روز رسانی
                if company is not None and company != existing[3]: updates.append("company = %s"); params.append(company)
                if industry is not None and industry != existing[4]: updates.append("industry = %s"); params.append(industry)
                if services is not None and services != existing[5]: updates.append("services = %s"); params.append(services)
                
                if updates:
                    query = f"UPDATE customers SET {', '.join(updates)} WHERE id = %s"
                    params.append(existing[0])
                    cursor.execute(query, tuple(params))
                    return f"اطلاعات مشتری '{name}' با موفقیت به‌روزرسانی شد."
                else:
                    return f"مشتری '{name}' قبلاً ثبت شده و اطلاعات جدیدی برای به‌روزرسانی وجود نداشت."
            else:
                # ثبت مشتری جدید
                cursor.execute(
                    "INSERT INTO customers (name, phone, company, industry, services) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, phone, company, industry, services)
                )
                new_id = cursor.fetchone()[0]
                return f"عملیات ثبت مشتری موفق بود. مشتری '{name}' (ID: {new_id}) با موفقیت ثبت شد."
    except psycopg2.Error as e:
        if e.pgcode == '23505': # خطای Unique Violation (شماره تلفن تکراری)
            return f"خطا: شماره تلفن '{phone}' قبلاً برای مشتری دیگری ثبت شده است."
        return f"خطای دیتابیس در ثبت مشتری: {e}"
    except Exception as e:
        return f"خطای ناشناخته در ثبت مشتری: {e}"

def log_interaction(customer_name: str, interaction_report: str, follow_up_date: str = None) -> str:
    """ثبت گزارش تماس یا تعامل جدید با یک مشتری موجود. (قابلیت ۲)"""
    conn = get_db_connection()
    if conn is None:
        return "خطا: سرویس حافظه دائمی (PostgreSQL) فعال نیست."
        
    customer = find_customer_data(customer_name)
    
    if not customer:
        return f"خطا: مشتری با نام '{customer_name}' در دیتابیس پیدا نشد. لطفا ابتدا او را ثبت کنید."

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO interactions (customer_name, interaction_date, report, follow_up_date) VALUES (%s, %s, %s, %s) RETURNING id",
                (customer_name, TODAY_DATE, interaction_report, follow_up_date)
            )
            new_id = cursor.fetchone()[0]
            follow_up_msg = f"پیگیری بعدی برای تاریخ {follow_up_date} تنظیم شد." if follow_up_date else ""
            return f"گزارش تماس با '{customer_name}' با موفقیت در دیتابیس ثبت شد. (ID: {new_id}). {follow_up_msg}"
    except Exception as e:
        return f"خطا در ثبت گزارش تعامل: {e}"

def set_reminder(customer_name: str, reminder_text: str, date_time: str, chat_id: int) -> str:
    """ثبت یک یادآوری یا هشدار. (قابلیت ۳)"""
    conn = get_db_connection()
    if conn is None:
        return "خطا: سرویس حافظه دائمی (PostgreSQL) فعال نیست."
    try:
        # تاریخ و زمان را به فرمت قابل قبول PostgreSQL تبدیل می کند
        # فرض می کنیم Gemini تاریخ و زمان را به شکل YYYY-MM-DD HH:MM می دهد
        parsed_datetime = datetime.strptime(date_time, "%Y-%m-%d %H:%M")
        
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO reminders (chat_id, customer_name, reminder_text, due_date_time) VALUES (%s, %s, %s, %s) RETURNING id",
                (chat_id, customer_name, reminder_text, parsed_datetime)
            )
            new_id = cursor.fetchone()[0]
            return f"هشدار با متن '{reminder_text[:30]}...' برای {date_time} با موفقیت در دیتابیس ثبت شد. (ID: {new_id})"
    except ValueError:
        # اگر فرمت تاریخ اشتباه باشد (خطای قبلی شما)
        return "خطا: فرمت تاریخ و زمان هشدار باید به شکل YYYY-MM-DD HH:MM باشد."
    except Exception as e:
        return f"خطا در ثبت هشدار: {e}"

def get_report(query_type: str, search_term: str = None, fields: str = "all") -> str:
    """دریافت گزارش یا اطلاعات خاصی از مشتریان. (قابلیت ۴)"""
    conn = get_db_connection()
    if conn is None:
        return "خطا: سرویس حافظه دائمی (PostgreSQL) فعال نیست."
        
    try:
        with conn.cursor() as cursor:
            if query_type == 'industry_search' and search_term:
                # گزارش فیلتر شده بر اساس صنعت
                field_names = [f.strip() for f in fields.split(',')] if fields != "all" else ["name", "phone", "company", "industry"]
                
                cursor.execute(f"SELECT {', '.join(field_names)} FROM customers WHERE industry ILIKE %s", (f"%{search_term}%",))
                results = cursor.fetchall()
                
                if not results:
                    return f"هیچ مشتری در حوزه '{search_term}' پیدا نشد."
                    
                output = [f"مشتریان در حوزه '{search_term}' (فیلدهای: {', '.join(field_names)}):\n", " | ".join(field_names), "-" * 50]
                output.extend([" | ".join(map(str, row)) for row in results])
                return "\n".join(output)
                
            elif query_type == 'full_customer' and search_term:
                # جزئیات کامل مشتری و تعاملات
                cursor.execute("SELECT id, name, phone, company, industry, services FROM customers WHERE name ILIKE %s", (search_term,))
                customer = cursor.fetchone()
                
                if not customer:
                    return f"خطا: مشتری با نام '{search_term}' پیدا نشد."
                    
                customer_data = {
                    "ID": customer[0], "Name": customer[1], "Phone": customer[2], 
                    "Company": customer[3], "Industry": customer[4], "Services": customer[5]
                }
                output = ["جزئیات مشتری (از PostgreSQL):\n" + json.dumps(customer_data, ensure_ascii=False, indent=2)]

                # جستجوی تعاملات
                cursor.execute("SELECT interaction_date, report, follow_up_date FROM interactions WHERE customer_name ILIKE %s ORDER BY interaction_date DESC", (search_term,))
                interactions = cursor.fetchall()
                
                if interactions:
                    output.append("\nگزارشات تعامل:\n")
                    for interaction in interactions:
                        # تبدیل تاریخ از شیء Date به رشته برای نمایش
                        date = interaction[0].strftime("%Y-%m-%d") if interaction[0] else 'N/A'
                        report = interaction[1]
                        follow_up = interaction[2].strftime("%Y-%m-%d") if interaction[2] else 'ندارد'
                        output.append(f"  - تاریخ: {date}, پیگیری: {follow_up}\n    خلاصه: {report[:100]}...")
                else:
                    output.append("هیچ گزارش تعاملی ثبت نشده است.")
                
                return "\n".join(output)
                
            return f"نوع گزارش '{query_type}' پشتیبانی نمی‌شود یا عبارت جستجو مشخص نیست."
    except Exception as e:
        logger.error(f"Error getting report: {e}")
        return f"خطای دیتابیس هنگام گزارش‌گیری: {e}"

# =================================================================
# --- توابع مدیریت تلگرام (قابلیت ۵ و ۶) ---
# =================================================================

async def export_data_to_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تولید فایل CSV از اطلاعات کامل مشتریان و ارسال آن به کاربر (قابلیت ۵)."""
    conn = get_db_connection()
    if conn is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ سرویس حافظه دائمی (PostgreSQL) فعال نیست.")
        return
        
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    
    try:
        with conn.cursor() as cursor:
            # خواندن تمام داده‌ها از جدول مشتریان
            cursor.execute("SELECT * FROM customers")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            
            if not rows:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ دیتابیس مشتریان خالی است. فایلی برای ارسال وجود ندارد.")
                return

            # تولید محتوای CSV
            csv_content = [",".join(columns)]
            for row in rows:
                # جایگزینی کاما با نقطه ویرگول برای جلوگیری از بهم ریختگی CSV
                safe_row = [str(item).replace(',', ';') if item else '' for item in row]
                csv_content.append(",".join(safe_row))
                
            file_name = f"CRM_Customers_Export_{TODAY_DATE}.csv"
            
            # ارسال فایل (با استفاده از utf-8 برای پشتیبانی از فارسی)
            await context.bot.send_document(
                chat_id=chat_id, 
                document=bytes("\n".join(csv_content).encode('utf-8')),
                filename=file_name,
                caption="فایل کامل مشتریان CRM (حافظه دائمی PostgreSQL) با فرمت CSV"
            )
    except Exception as e:
        logger.error(f"Error exporting data from PostgreSQL: {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ خطایی هنگام استخراج داده‌ها از دیتابیس رخ داد.")

# =================================================================
# --- وظیفه بک‌گراند برای هشدارها (قابلیت ۳: ارسال خودکار پیام) ---
# =================================================================

async def reminder_checker(application: Application):
    """وظیفه دوره‌ای برای بررسی و ارسال هشدارهای ثبت شده."""
    
    while True:
        await asyncio.sleep(60) # هر ۶۰ ثانیه یک بار چک می‌کند
        
        conn = get_db_connection() # اتصال را در داخل حلقه چک می کنیم
        if conn is None:
            logger.warning("Reminder checker skipped: PostgreSQL not initialized.")
            continue
            
        try:
            with conn.cursor() as cursor:
                # خواندن هشدارهایی که هنوز ارسال نشده و زمان آن‌ها گذشته یا رسیده است
                cursor.execute(
                    "SELECT id, chat_id, customer_name, reminder_text FROM reminders WHERE sent = FALSE AND due_date_time <= NOW()"
                )
                reminders_to_send = cursor.fetchall()
                
                for reminder in reminders_to_send:
                    r_id, chat_id, customer_name, reminder_text = reminder
                    
                    # ارسال پیام هشدار
                    message = f"🔔 **هشدار CRM**\n\nمشتری: **{customer_name or 'عمومی'}**\nپیام: _{reminder_text}_\n\n"
                    await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                    
                    # به‌روزرسانی وضعیت ارسال
                    with conn.cursor() as update_cursor:
                        update_cursor.execute("UPDATE reminders SET sent = TRUE WHERE id = %s", (r_id,))
                    
        except Exception as e:
            logger.error(f"Failed to run reminder checker: {e}")

# =================================================================
# --- تابع اصلی هندلر پیام و اجرا (Main Execution Function) ---
# =================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ai_client or not update.message or not update.message.text:
        return

    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    # بررسی دکمه‌های آماده (قابلیت ۶)
    if user_text.strip() == "📥 ارسال فایل کل مشتریان":
        await export_data_to_file(update, context)
        return
    
    # --- ۱. مدیریت حافظه مکالمه (Conversation History) ---
    if 'history' not in context.user_data:
        context.user_data['history'] = []
    
    user_part = types.Part(text=user_text)
    context.user_data['history'].append(types.Content(role="user", parts=[user_part]))
    conversation_history = context.user_data['history']
    
    # تعریف پرامپت سیستمی (System Instruction) (قابلیت ۷)
    system_instruction = (
        "شما یک دستیار هوشمند CRM با **حافظه کامل (PostgreSQL)** و تحلیلگر هوشمند هستید. "
        "وظایف شما: ۱. ثبت، به‌روزرسانی، حذف، ثبت گزارش‌ها و تنظیم هشدارها با استفاده از توابع (Tools). "
        "۲. ارائه گزارش هوشمند و فیلتر شده (قابلیت ۴). "
        "۳. **تحلیل هوشمند و ارائه پیشنهاد عملی (قابلیت ۷):** پس از اجرای موفقیت‌آمیز هر تابع **ثبت/حذف**، باید داده‌های جدید و تاریخچه را تحلیل کنید و **به صورت یک پاراگراف جداگانه**، یک پیشنهاد عملی (Actionable Advice) برای پیگیری بعدی یا بهبود روند فروش ارائه دهید (مانند بهترین زمان تماس، پیشنهادات رقابتی، یا مراحل بعدی). "
        "**قوانین:** 1. هرگاه داده‌های اجباری برای یک تابع جمع‌آوری شد، آن را فراخوانی کنید. 2. همیشه پاسخ های خود را به زبان فارسی و دوستانه بنویسید. 3. در فراخوانی تابع set_reminder، 'chat_id' را برابر با **" + str(chat_id) + "** قرار دهید."
    )

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    try:
        # مرحله ۱: ارسال درخواست با تاریخچه مکالمه
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=conversation_history, 
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                # اضافه کردن تابع حذف (delete_customer)
                tools=[manage_customer_data, log_interaction, set_reminder, get_report, delete_customer]
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
                
                if function_name == 'manage_customer_data': tool_result = manage_customer_data(**args)
                elif function_name == 'log_interaction': tool_result = log_interaction(**args)
                elif function_name == 'set_reminder':
                    if 'chat_id' not in args: args['chat_id'] = chat_id 
                    tool_result = set_reminder(**args)
                elif function_name == 'get_report': tool_result = get_report(**args)
                elif function_name == 'delete_customer': tool_result = delete_customer(**args)
                else: tool_result = f"خطا: تابع {function_name} ناشناخته است."
                    
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
                    tools=[manage_customer_data, log_interaction, set_reminder, get_report, delete_customer]
                )
            )
            
            if final_response.candidates and final_response.candidates[0].content:
                context.user_data['history'].append(final_response.candidates[0].content)

            await update.message.reply_text(final_response.text, parse_mode='Markdown')

        else:
            if response.candidates and response.candidates[0].content:
                context.user_data['history'].append(response.candidates[0].content)
            await update.message.reply_text(response.text, parse_mode='Markdown')

    except APIError as e:
        logger.error(f"Gemini API Error: {e}")
        await update.message.reply_text("⚠️ خطای API رخ داد. لطفاً چند دقیقه دیگر امتحان کنید.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        await update.message.reply_text(f"❓ یک خطای نامشخص رخ داد. لطفاً لاگ‌های سرور را بررسی کنید. خطا: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پاسخ به دستور /start و راهنمایی اولیه."""
    
    if 'history' in context.user_data:
        del context.user_data['history']
        
    # بررسی وضعیت اتصال AI
    ai_status = "✅ متصل و آماده" if ai_client else "❌ غیرفعال (کلید API را بررسی کنید)."
    
    # بررسی وضعیت اتصال دیتابیس
    conn = get_db_connection()
    db_status = "✅ متصل به PostgreSQL" if conn else "❌ مشکل در اتصال به دیتابیس"
    if conn: conn.close() # بستن اتصال موقت
    
    reply_keyboard = [
        ["✍️ ثبت اطلاعات جدید", "📞 ثبت گزارش تماس"],
        ["📊 درخواست گزارش هوشمند", "📥 ارسال فایل کل مشتریان"],
    ]
    markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=False, resize_keyboard=True)
    
    message = (
        f"🤖 **CRM Bot هوشمند با حافظه دائمی PostgreSQL**\n\n"
        f"✨ وضعیت AI: {ai_status}\n"
        f"💾 وضعیت حافظه: {db_status}\n"
        f"**نحوه استفاده:** هرگونه پیام یا درخواستی که دارید را ارسال کنید، یا از دکمه‌های زیر استفاده کنید. ربات نیت شما را درک و عملیات لازم را انجام می‌دهد و **پیشنهاد هوشمندانه** می‌دهد.\n\n"
        f"**مثال‌های هوشمند:**\n"
        f" - **ثبت و تحلیل:** 'با آقای نوری صحبت کردم. گفت قیمت رقبا بالاتره.'\n"
        f" - **هشدار فعال:** 'برای هفته بعد دوشنبه ساعت ۱۰ صبح پیگیری با نوری رو برام یادآوری کن.' (تاریخ باید به فرمت YYYY-MM-DD HH:MM باشد.)\n"
        f" - **حذف:** 'آقای الف رو از لیست مشتریان حذف کن.'\n"
    )
    
    await update.message.reply_text(message, reply_markup=markup, parse_mode='Markdown')


def main() -> None:
    """شروع به کار ربات (با منطق انتخاب Webhook یا Polling)"""
    
    global ai_client
    if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_API_KEY_HERE":
        try:
            ai_client = genai.Client(api_key=GEMINI_API_KEY)
            logger.info("Gemini Client and Model Initialized Successfully.")
        except Exception as e:
            logger.error(f"Error initializing Gemini client: {e}")
            
    else:
        logger.error("GEMINI_API_KEY is not set.")

    # --- ابتدا اتصال به PostgreSQL را برقرار و جداول را می‌سازیم (نقطه حیاتی برای حافظه دائمی) ---
    if init_db():
        logger.info("PostgreSQL Database is ready for use.")
    else:
        logger.error("FATAL: Could not initialize PostgreSQL. Check DATABASE_URL and Render service.")

    
    if RENDER_EXTERNAL_URL and TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        # --- اجرای Webhook ---
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        
        # اجرای وظیفه یادآوری (Reminders) به صورت خودکار
        if application.job_queue:
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
    
    if application.job_queue:
        application.job_queue.run_once(
            lambda context: asyncio.create_task(reminder_checker(application)),
            0
        )

    logger.info("Starting Memory-Enabled Free-Form CRM Bot (Polling Mode)...")
    application.run_polling(poll_interval=3.0)
    
if __name__ == "__main__":
    main()
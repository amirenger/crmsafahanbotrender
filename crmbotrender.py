import os
import logging
from datetime import datetime
import json
import asyncio
import gspread # <--- جدید
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
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID") # <--- جدید
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS") # <--- جدید

# متغیرهای Webhook/Render
PORT = int(os.environ.get('PORT', '8000'))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
# =================================================================

# --- آماده‌سازی هوش مصنوعی و ثابت‌ها ---
ai_client = None
AI_MODEL = 'gemini-2.5-flash'
TODAY_DATE = datetime.now().strftime("%Y-%m-%d")

# --- آماده‌سازی Google Sheets ---
CUSTOMER_SHEET_NAME = "Customers"
INTERACTION_SHEET_NAME = "Interactions"
REMINDER_SHEET_NAME = "Reminders"
gs_client = None
gs_customer_sheet = None
gs_interaction_sheet = None
gs_reminder_sheet = None

# --- توابع اتصال و آماده سازی Sheets ---

def init_sheets():
    """اتصال به Google Sheets و باز کردن ورک‌شیت‌ها."""
    global gs_client, gs_customer_sheet, gs_interaction_sheet, gs_reminder_sheet
    
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        logger.error("Google Sheet ID or Credentials not set. Persistent memory disabled.")
        return False
        
    try:
        # Load credentials from environment variable
        creds = json.loads(GOOGLE_CREDENTIALS_JSON)
        
        # Authenticate with gspread
        gs_client = gspread.service_account_from_dict(creds)
        spreadsheet = gs_client.open_by_key(GOOGLE_SHEET_ID)
        
        # Initialize worksheets
        try:
            gs_customer_sheet = spreadsheet.worksheet(CUSTOMER_SHEET_NAME)
        except gspread.WorksheetNotFound:
            gs_customer_sheet = spreadsheet.add_worksheet(title=CUSTOMER_SHEET_NAME, rows=1000, cols=7)
            gs_customer_sheet.append_row(["ID", "Name", "Phone", "Company", "Industry", "Services", "CRM User ID"])

        try:
            gs_interaction_sheet = spreadsheet.worksheet(INTERACTION_SHEET_NAME)
        except gspread.WorksheetNotFound:
            gs_interaction_sheet = spreadsheet.add_worksheet(title=INTERACTION_SHEET_NAME, rows=1000, cols=5)
            gs_interaction_sheet.append_row(["Interaction ID", "Customer Name", "Interaction Date", "Report", "Follow Up Date"])

        try:
            gs_reminder_sheet = spreadsheet.worksheet(REMINDER_SHEET_NAME)
        except gspread.WorksheetNotFound:
            gs_reminder_sheet = spreadsheet.add_worksheet(title=REMINDER_SHEET_NAME, rows=1000, cols=6)
            gs_reminder_sheet.append_row(["Reminder ID", "Chat ID", "Customer Name", "Reminder Text", "Due Date Time", "Sent"])

        logger.info("Google Sheets Client Initialized Successfully. Persistent memory is now ON.")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets: {e}")
        return False

# --- توابع (Functions) که هوش مصنوعی به آنها دسترسی دارد (Tools) ---
# توابع زیر اکنون با Google Sheets کار می‌کنند.

def find_customer_row(name: str, phone: str = None) -> (dict, int):
    """جستجوی مشتری بر اساس نام و/یا تلفن و بازگرداندن دیکشنری داده‌ها و شماره سطر."""
    if not gs_customer_sheet: return None, None
    
    # برای جستجو، تمام رکوردهای مشتریان را می‌خوانیم
    data = gs_customer_sheet.get_all_records()
    
    for index, row in enumerate(data):
        # gspread index: index + 2 (Header row + 1-based index)
        row_num = index + 2 
        
        # تطبیق نام (case-insensitive)
        name_match = row['Name'].strip().lower() == name.strip().lower()
        
        # اگر تلفن داده شده، باید آن هم تطبیق یابد
        phone_match = True
        if phone:
            phone_match = row['Phone'].strip() == phone.strip()

        if name_match and phone_match:
            return row, row_num
            
    # اگر فقط با نام تطبیق دهیم
    if not phone:
        for index, row in enumerate(data):
            row_num = index + 2
            if row['Name'].strip().lower() == name.strip().lower():
                 return row, row_num
                 
    return None, None


def manage_customer_data(name: str, phone: str, company: str = None, industry: str = None, services: str = None) -> str:
    """ثبت مشتری جدید یا به‌روزرسانی اطلاعات مشتری موجود. (قابلیت ۱ و ۲)"""
    if not gs_customer_sheet:
        return "خطا: سرویس حافظه دائمی (Google Sheets) فعال نیست."
    if not name or not phone:
        return "خطا: نام و شماره تلفن برای ثبت یا به‌روزرسانی مشتری الزامی هستند."

    customer, row_num = find_customer_row(name, phone)
    
    if customer:
        # به‌روزرسانی مشتری موجود
        updates = {}
        if company and company != customer['Company']: updates['Company'] = company
        if industry and industry != customer['Industry']: updates['Industry'] = industry
        if services and services != customer['Services']: updates['Services'] = services
        
        if updates:
            # ستون‌های قابل به‌روزرسانی: Company (4), Industry (5), Services (6)
            headers = gs_customer_sheet.row_values(1)
            
            for key, value in updates.items():
                col_index = headers.index(key) + 1 # 1-based index
                gs_customer_sheet.update_cell(row_num, col_index, value)
            
            return f"اطلاعات مشتری '{name}' (ID: {customer['ID']}) با موفقیت به‌روزرسانی شد."
        else:
            return f"مشتری '{name}' (ID: {customer['ID']}) قبلاً ثبت شده و اطلاعات جدیدی برای به‌روزرسانی وجود نداشت."
    else:
        # ثبت مشتری جدید
        
        # تولید ID جدید بر اساس آخرین سطر
        all_ids = gs_customer_sheet.col_values(1)[1:] 
        new_id = int(all_ids[-1]) + 1 if all_ids and all_ids[-1].isdigit() else 1
        
        try:
            new_row = [new_id, name, phone, company or '', industry or '', services or '', 0]
            gs_customer_sheet.append_row(new_row)
            return f"عملیات ثبت مشتری موفق بود. مشتری '{name}' (ID: {new_id}) با موفقیت ثبت شد."
        except Exception as e:
            return f"خطای ناشناخته در ثبت مشتری در شیت: {e}"


def log_interaction(customer_name: str, interaction_report: str, follow_up_date: str = None) -> str:
    """ثبت گزارش تماس یا تعامل جدید با یک مشتری موجود. (قابلیت ۲)"""
    if not gs_interaction_sheet:
        return "خطا: سرویس حافظه دائمی (Google Sheets) فعال نیست."
        
    customer, _ = find_customer_row(customer_name)
    
    if not customer:
        return f"خطا: مشتری با نام '{customer_name}' در دیتابیس پیدا نشد. لطفا ابتدا او را ثبت کنید."

    try:
        # تولید ID جدید بر اساس آخرین سطر
        all_ids = gs_interaction_sheet.col_values(1)[1:] 
        new_id = int(all_ids[-1]) + 1 if all_ids and all_ids[-1].isdigit() else 1
        
        new_row = [
            new_id, 
            customer_name, 
            TODAY_DATE, 
            interaction_report, 
            follow_up_date or ''
        ]
        gs_interaction_sheet.append_row(new_row)
        
        follow_up_msg = f"پیگیری بعدی برای تاریخ {follow_up_date} تنظیم شد." if follow_up_date else ""
        return f"گزارش تماس با '{customer_name}' با موفقیت در Google Sheets ثبت شد. {follow_up_msg}"
    except Exception as e:
        return f"خطا در ثبت گزارش تعامل در شیت: {e}"


def set_reminder(customer_name: str, reminder_text: str, date_time: str, chat_id: int) -> str:
    """ثبت یک یادآوری یا هشدار. (قابلیت ۳)"""
    if not gs_reminder_sheet:
        return "خطا: سرویس حافظه دائمی (Google Sheets) فعال نیست."
    try:
        # تولید ID جدید
        all_ids = gs_reminder_sheet.col_values(1)[1:] 
        new_id = int(all_ids[-1]) + 1 if all_ids and all_ids[-1].isdigit() else 1
        
        new_row = [new_id, chat_id, customer_name, reminder_text, date_time, 0] # 0 for Sent status (Not Sent)
        gs_reminder_sheet.append_row(new_row)
        
        return f"هشدار با متن '{reminder_text[:30]}...' برای {date_time} با موفقیت در Google Sheets ثبت شد."
    except Exception as e:
        return f"خطا در ثبت هشدار در شیت: {e}"


def get_report(query_type: str, search_term: str = None, fields: str = "all") -> str:
    """دریافت گزارش یا اطلاعات خاصی از مشتریان. (قابلیت ۴)"""
    if not gs_customer_sheet or not gs_interaction_sheet:
        return "خطا: سرویس حافظه دائمی (Google Sheets) فعال نیست."
        
    customer_data = gs_customer_sheet.get_all_records()
    
    if query_type == 'industry_search' and search_term:
        results = []
        field_names = [f.strip() for f in fields.split(',')] if fields != "all" else ["Name", "Phone", "Company", "Industry"]
        
        for customer in customer_data:
            if search_term.lower() in str(customer.get('Industry', '')).lower():
                row_data = [str(customer.get(field, '')) for field in field_names]
                results.append(" | ".join(row_data))
                
        if not results:
            return f"هیچ مشتری در حوزه '{search_term}' پیدا نشد."
            
        output = [f"مشتریان در حوزه '{search_term}' (فیلدهای: {', '.join(field_names)}):\n", " | ".join(field_names), "-" * 50]
        output.extend(results)
        return "\n".join(output)
        
    elif query_type == 'full_customer' and search_term:
        customer, _ = find_customer_row(search_term)
        
        if not customer:
            return f"خطا: مشتری با نام '{search_term}' پیدا نشد."
            
        output = ["جزئیات مشتری (از Google Sheets):\n" + json.dumps(customer, ensure_ascii=False, indent=2)]

        # جستجوی تعاملات
        interaction_data = gs_interaction_sheet.get_all_records()
        interactions = [
            i for i in interaction_data 
            if str(i.get('Customer Name', '')).strip().lower() == search_term.strip().lower()
        ]
        
        if interactions:
            output.append("\nگزارشات تعامل:\n")
            for interaction in interactions:
                date = interaction.get('Interaction Date', '')
                report = interaction.get('Report', '')
                follow_up = interaction.get('Follow Up Date', 'ندارد')
                output.append(f"  - تاریخ: {date}, پیگیری: {follow_up}\n    خلاصه: {report[:100]}...")
        else:
            output.append("هیچ گزارش تعاملی ثبت نشده است.")
        
        return "\n".join(output)
        
    return f"نوع گزارش '{query_type}' پشتیبانی نمی‌شود یا عبارت جستجو مشخص نیست."

# =================================================================
# --- توابع مدیریت تلگرام (قابلیت ۵ و ۶) ---
# =================================================================

async def export_data_to_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تولید فایل CSV از اطلاعات کامل مشتریان و ارسال آن به کاربر (قابلیت ۵)."""
    if not gs_customer_sheet:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ سرویس حافظه دائمی (Google Sheets) فعال نیست.")
        return
        
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    
    try:
        # دریافت تمام داده‌ها شامل هدر (با متد get_all_values)
        all_data = gs_customer_sheet.get_all_values()
        
        if len(all_data) <= 1: # فقط شامل هدر است
            await context.bot.send_message(chat_id=chat_id, text="⚠️ دیتابیس مشتریان خالی است. فایلی برای ارسال وجود ندارد.")
            return

        # تولید محتوای CSV
        csv_content = []
        for row in all_data:
            # جایگزینی کاما با نقطه ویرگول برای جلوگیری از بهم ریختگی CSV
            safe_row = [str(item).replace(',', ';') if item else '' for item in row]
            csv_content.append(",".join(safe_row))
            
        file_name = f"CRM_Customers_Export_{TODAY_DATE}.csv"
        
        # ارسال فایل (با استفاده از utf-8 برای پشتیبانی از فارسی)
        await context.bot.send_document(
            chat_id=chat_id, 
            document=bytes("\n".join(csv_content).encode('utf-8')),
            filename=file_name,
            caption="فایل کامل مشتریان CRM (حافظه دائمی Google Sheets) با فرمت CSV"
        )
    except Exception as e:
        logger.error(f"Error exporting data from sheet: {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ خطایی هنگام استخراج داده‌ها از Google Sheets رخ داد.")

# =================================================================
# --- وظیفه بک‌گراند برای هشدارها (قابلیت ۳) ---
# =================================================================

async def reminder_checker(application: Application):
    """وظیفه دوره‌ای برای بررسی و ارسال هشدارهای ثبت شده."""
    if not gs_reminder_sheet:
        logger.warning("Reminder checker skipped: Google Sheets not initialized.")
        return
        
    while True:
        await asyncio.sleep(60) # هر ۶۰ ثانیه یک بار چک می‌کند
        
        try:
            # خواندن تمام داده‌های هشدارها
            reminders_data = gs_reminder_sheet.get_all_records()
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M") 
            
            for index, reminder in enumerate(reminders_data):
                # gspread index: index + 2
                row_num = index + 2 
                
                # تطبیق زمان تا دقیقه و بررسی وضعیت ارسال
                due_time = str(reminder.get('Due Date Time', ''))
                sent_status = int(reminder.get('Sent', 0))
                
                if sent_status == 0 and due_time.startswith(current_time_str):
                    
                    chat_id = int(reminder.get('Chat ID', 0))
                    customer_name = reminder.get('Customer Name', 'N/A')
                    reminder_text = reminder.get('Reminder Text', 'N/A')

                    # ارسال پیام هشدار
                    message = f"🔔 **هشدار CRM**\n\nمشتری: **{customer_name or 'عمومی'}**\nپیام: _{reminder_text}_\n\n"
                    await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                    
                    # به‌روزرسانی وضعیت ارسال در ستون 'Sent' (ستون ۶)
                    gs_reminder_sheet.update_cell(row_num, 6, 1) # Set Sent status to 1
                    
        except Exception as e:
            logger.error(f"Failed to run reminder checker: {e}")

# =================================================================
# --- تابع اصلی هندلر پیام و اجرا (Main Execution Function) ---
# =================================================================

# (تابع message_handler و start_command نیازی به تغییر عمده ندارند و همان منطق قبلی را دنبال می‌کنند)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (کد message_handler عیناً مشابه نسخه قبلی) ...
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
        "شما یک دستیار هوشمند CRM با **حافظه کامل (Google Sheets)** و تحلیلگر هوشمند هستید. "
        "وظایف شما: ۱. ثبت و به‌روزرسانی دقیق داده‌ها، ثبت گزارش‌ها و تنظیم هشدارها با استفاده از توابع (Tools). "
        "۲. ارائه گزارش هوشمند و فیلتر شده (قابلیت ۴). "
        "۳. **تحلیل هوشمند و ارائه پیشنهاد عملی (قابلیت ۷):** پس از اجرای موفقیت‌آمیز هر تابع **ثبت**، باید داده‌های جدید و تاریخچه را تحلیل کنید و **به صورت یک پاراگراف جداگانه**، یک پیشنهاد عملی (Actionable Advice) برای پیگیری بعدی یا بهبود روند فروش ارائه دهید (مانند بهترین زمان تماس، پیشنهادات رقابتی، یا مراحل بعدی). "
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
                tools=[manage_customer_data, log_interaction, set_reminder, get_report]
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
                    tools=[manage_customer_data, log_interaction, set_reminder, get_report]
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
    # init_sheets() # فراخوانی در main انجام می شود
    if 'history' in context.user_data:
        del context.user_data['history']
        
    ai_status = "✅ متصل و آماده" if ai_client else "❌ غیرفعال (کلید API را بررسی کنید)."
    sheet_status = "✅ متصل به Google Sheets" if gs_client else "❌ مشکل در اتصال به Google Sheets"
    
    reply_keyboard = [
        ["✍️ ثبت اطلاعات جدید", "📞 ثبت گزارش تماس"],
        ["📊 درخواست گزارش هوشمند", "📥 ارسال فایل کل مشتریان"],
    ]
    markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=False, resize_keyboard=True)
    
    message = (
        f"🤖 **CRM Bot هوشمند با حافظه دائمی Google Sheets**\n\n"
        f"✨ وضعیت AI: {ai_status}\n"
        f"💾 وضعیت حافظه: {sheet_status}\n"
        f"**نحوه استفاده:** هرگونه پیام یا درخواستی که دارید را ارسال کنید، یا از دکمه‌های زیر استفاده کنید. ربات نیت شما را درک و عملیات لازم را انجام می‌دهد و **پیشنهاد هوشمندانه** می‌دهد.\n\n"
        f"**مثال‌های هوشمند:**\n"
        f" - **ثبت و تحلیل:** 'با آقای نوری صحبت کردم. گفت قیمت رقبا بالاتره.'\n"
        f" - **هشدار:** 'برای هفته بعد دوشنبه ساعت ۱۰ صبح پیگیری با نوری رو برام یادآوری کن.'\n"
    )
    
    await update.message.reply_text(message, reply_markup=markup, parse_mode='Markdown')


def main() -> None:
    """شروع به کار ربات (با منطق انتخاب Webhook یا Polling)"""
    
    # --- ابتدا اتصال به Google Sheets را برقرار می کنیم ---
    if not init_sheets():
        logger.error("FATAL: Could not initialize Google Sheets. Bot cannot run without persistent memory.")
        # اگر اتصال برقرار نشود، ربات اجرا نخواهد شد
        # این باعث می شود Render وضعیت Down را نشان دهد، که هدف ما نیست، اما داده ها از دست نمی رود.
        # برای ادامه کار، ما اجازه می دهیم تا application ساخته شود و خطا را در start_command نشان می دهیم.

    # ... (بقیه کد main() مشابه نسخه قبلی) ...
    
    if RENDER_EXTERNAL_URL and TELEGRAM_BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        # --- اجرای Webhook ---
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        
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
import os
import time
import string
import random
import telebot
import logging
from sqlite3 import connect
from flask import Flask, request, jsonify
from peewee import SqliteDatabase, Model, CharField, IntegerField, BooleanField
from concurrent.futures import ThreadPoolExecutor
from peewee import transaction

# Environment Variables
API_TOKEN = os.getenv('API_TOKEN', 'YOUR_TELEGRAM_BOT_API_TOKEN')
ADMIN_KEY = os.getenv('ADMIN_KEY', 'YOUR_ADMIN_KEY')
BASE_URL = os.getenv('BASE_URL', 'https://test.org/user/tgauth?key=')
GROUP_IDS = list(map(int, os.getenv('GROUP_IDS', 'YOUR_GROUP_ID').split(',')))
DB_NAME = 'data/users.db'

# Bot and Flask Setup
bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)
db = SqliteDatabase(DB_NAME)

# ThreadPoolExecutor for managing threads
executor = ThreadPoolExecutor(max_workers=10)

# Database Model
class User(Model):
    user_id = IntegerField(unique=True)
    uid = CharField(unique=True, null=True)
    validated = BooleanField(default=False)
    code = CharField(null=True)
    code_generated_time = IntegerField(null=True)

    class Meta:
        database = db

db.connect()
db.create_tables([User], safe=True)

# Utility Functions
def generate_code(size=6, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))

def generate_auth_url(code):
    return f"{BASE_URL}{code}"

def mask_uid(uid):
    if len(uid) < 4:
        return "元id"
    return uid[:1] + '*' * (len(uid) - 2) + uid[-1:]

def restrict_user_in_group(user_id, restrict=True):
    permissions = {
        'can_send_messages': not restrict,
        'can_send_media_messages': not restrict,
        'can_send_other_messages': not restrict,
        'can_add_web_page_previews': not restrict
    }
    for group_id in GROUP_IDS:
        try:
            bot.restrict_chat_member(chat_id=group_id, user_id=user_id, **permissions)
        except Exception as e:
            logging.error(f"Failed to modify user permissions: {e}")

# Flask API Route for Validation
@app.route('/api/validate', methods=['POST'])
def validate():
    data = request.json
    if data.get('admin_key') != ADMIN_KEY:
        return jsonify({'status': 'error', 'message': 'Invalid admin key'}), 403

    code = data.get('code')
    uid = data.get('uid')
    user = User.get_or_none(code=code)

    if not user or time.time() - user.code_generated_time > 600:
        return jsonify({'status': 'error', 'message': '验证码超时'}), 401

    # Ensure uid is unique and matches user_id
    if User.get_or_none(User.uid == uid, User.user_id != user.user_id):
        return jsonify({'status': 'error', 'message': '此UID已绑定其他Telegram账号'}), 403

    with db.atomic():
        user.validated = True
        user.uid = uid
        user.code = None
        user.code_generated_time = None
        user.save()

    restrict_user_in_group(user.user_id, restrict=False)
    try:
        bot.send_message(user.user_id, "验证成功, 禁言稍后自动解除，或者您也可以退群重新加载和私聊管理员")
    except Exception as e:
        logging.error(f'Failed to send message to user {user.user_id}: {e}')

    return jsonify({'status': 'success', 'message': '验证成功'})

# Telegram Bot Handlers
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.chat.type == 'private':
        bot.reply_to(message, "请发送 /auth 进行验证.")
    else:
        bot.delete_message(message.chat.id, message.message_id)

@bot.message_handler(commands=['auth'])
def handle_auth_command(message):
    if message.chat.type != 'private':
        bot.delete_message(message.chat.id, message.message_id)
        return

    user_id = message.from_user.id
    user, _ = User.get_or_create(user_id=user_id)

    if user.validated:
        bot.reply_to(message, f"你已经验证成功啦!, {user.uid}")
    else:
        if user.code and time.time() - user.code_generated_time < 600:
            auth_url = generate_auth_url(user.code)
        else:
            user.code = generate_code()
            user.code_generated_time = int(time.time())
            user.save()
            auth_url = generate_auth_url(user.code)
        bot.reply_to(message, f"请先在 {BASE_URL.split('://')[0]}://{BASE_URL.split('/')[2]} 登录您的账号，然后请点击此链接完成验证（连接十分钟内有效）: {auth_url}")

@bot.message_handler(content_types=["new_chat_members"])
def handle_new_member(message):
    if message.chat.id not in GROUP_IDS:
        return

    for new_member in message.new_chat_members:
        user_id = new_member.id
        user, _ = User.get_or_create(user_id=user_id)

        restrict_user_in_group(user_id)

        if user.validated:
            bot.send_message(message.chat.id, f"欢迎 {mask_uid(user.uid)}, {new_member.first_name}!")
            restrict_user_in_group(user_id, restrict=False)
        else:
            reply_msg = bot.send_message(message.chat.id, f"你没有进行用户验证,请私聊本机器人进行验证 @paoluzsc_bot {new_member.first_name}!")
            executor.submit(kick_if_not_verified, user_id, new_member.first_name, message.chat.id, reply_msg)

def kick_if_not_verified(user_id, user_name, chat_id, reply_msg):
    time.sleep(180)
    bot.delete_message(chat_id=chat_id, message_id=reply_msg.message_id)
    user = User.get_or_none(user_id=user_id)
    if user and not user.validated:
        bot.kick_chat_member(chat_id, user_id, until_date=int(time.time()) + 60)
        msg_tg = bot.send_message(chat_id, f"{user_name} 因未验证已被移出群组。")
        time.sleep(60)
        bot.delete_message(chat_id=chat_id, message_id=msg_tg.message_id)

# Flask App Starter
def start_flask_app():
    app.run(host='0.0.0.0', port=5000, threaded=True, timeout=60)

if __name__ == '__main__':
    Thread(target=start_flask_app).start()
    bot.polling()
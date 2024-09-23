import os
import time
import string
import random
import telebot
import logging
from sqlite3 import connect
from flask import Flask, request, jsonify
from peewee import SqliteDatabase, Model, CharField, IntegerField, BooleanField
from threading import Thread

API_TOKEN = os.getenv('API_TOKEN', 'YOUR_TELEGRAM_BOT_API_TOKEN')
ADMIN_KEY = os.getenv('ADMIN_KEY', 'YOUR_ADMIN_KEY')
BASE_URL = os.getenv('BASE_URL', 'https://test.org/user/tgauth?key=')
GROUP_IDS = list(map(int, os.getenv('GROUP_IDS', 'YOUR_GROUP_ID').split(',')))  # 从环境变量获取群组ID列表
DB_NAME = 'data/users.db'

NEED_AUTH_MSG = f"请先在 登录您的账号，然后请点击此链接完成验证（连接十分钟内有效）: "
WELCOME_AUTH_MSG = f"欢迎  用户"

bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

db = SqliteDatabase(DB_NAME)


class User(Model):
    user_id = IntegerField(unique=True)  # Telegram用户ID
    uid = CharField(unique=True, null=True)  # 平台的用户ID
    validated = BooleanField(default=False)
    code = CharField(null=True)
    code_generated_time = IntegerField(null=True)

    class Meta:
        database = db


db.connect()
db.create_tables([User], safe=True)


def generate_code(size=6, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


def generate_auth_url(code):
    return f"{BASE_URL}{code}"


@app.route('/api/validate', methods=['POST'])
def validate():
    data = request.json
    admin_key = data.get('admin_key')
    code = data.get('code')
    uid = data.get('uid')  # 获取传递的用户UID

    logging.warning(f'Received request with admin key: {admin_key}, code: {code}, uid: {uid}\n')
    if admin_key != ADMIN_KEY:
        return jsonify({'status': 'error', 'message': 'Invalid admin key'}), 403

    user = User.get_or_none(code=code)
    if not user or time.time() - user.code_generated_time > 600:
        logging.warning(f'Invalid or expired code: {code}\n')
        return jsonify({'status': 'error', 'message': '验证码超时'}), 401

    # 检查是否该uid或user_id已经绑定
    existing_user_with_uid = User.get_or_none(User.uid == uid)
    if existing_user_with_uid and existing_user_with_uid.user_id != user.user_id:
        return jsonify({'status': 'error', 'message': '此UID已绑定其他Telegram账号'}), 403

    existing_user_with_user_id = User.get_or_none((User.user_id == user.user_id) & User.uid.is_null(False))
    if existing_user_with_user_id and existing_user_with_user_id.uid != uid:
        return jsonify({'status': 'error', 'message': '此Telegram账号已绑定其他UID'}), 403

    # 更新用户的UID并设置为已验证
    user.validated = True
    user.uid = uid
    user.code = None
    user.code_generated_time = None
    user.save()

    for group_id in GROUP_IDS:
        try:
            bot.restrict_chat_member(
                chat_id=group_id,
                user_id=user.user_id,
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        except Exception as e:
            logging.error(e)
    try:
        bot.send_message(user.user_id, "验证成功, 禁言稍后自动解除，或者您也可以退群重新加载和私聊管理员")
    except Exception as e:
        logging.error(f'Failed to send message to user {user.user_id}: {e}')

    logging.warning(f'User {user.user_id} validated and unmuted\n')
    return jsonify({'status': 'success', 'message': '验证成功'})


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.chat.type != 'private':
        bot.delete_message(message.chat.id, message.message_id)
        return
    bot.reply_to(message, "请发送 /auth 进行验证.")


@bot.message_handler(commands=['groupid'])
def send_group_id(message):
    chat_id = message.chat.id
    bot.reply_to(message, f"The current group ID is: {chat_id}")


@bot.message_handler(commands=['auth'])
def handle_auth_command(message):
    if message.chat.type != 'private':
        bot.delete_message(message.chat.id, message.message_id)
        return
    user_id = message.from_user.id
    user, created = User.get_or_create(user_id=user_id)

    if user.validated:
        bot.reply_to(message, f"你已经验证成功啦!,{user.uid}")
    else:
        # 如果还在十分钟内直接返回当前code
        if user.code and time.time() - user.code_generated_time < 600:
            auth_url = generate_auth_url(user.code)
            bot.reply_to(message, f"{NEED_AUTH_MSG} {auth_url}")
            return

        code = generate_code()
        user.code = code
        user.code_generated_time = int(time.time())
        user.save()

        auth_url = generate_auth_url(code)
        bot.reply_to(message, f"{NEED_AUTH_MSG} {auth_url}")


def mask_uid(uid):
    if len(uid) < 4:
        return "元id"
    # 保留第一位和最后一位，其余用星号替换
    return uid[:1] + '*' * (len(uid) - 2) + uid[-1:]


@bot.message_handler(content_types=["new_chat_members"])
def handle_new_member(message):
    if message.chat.id not in GROUP_IDS:
        logging.warning(f'unknown group id {message.chat.id}\n')
        return  # 只处理指定群组
    for new_member in message.new_chat_members:
        user_id = new_member.id
        user, created = User.get_or_create(user_id=user_id)

        logging.warning(f'New member joined: {new_member.first_name} ({user_id})')

        try:
            bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user_id,
                until_date=0,  # 禁言
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False
            )
        except Exception as e:
            logging.error(f'Failed to restrict new member: {e}')

        if user.validated:
            bot.send_message(message.chat.id, f"{WELCOME_AUTH_MSG} {mask_uid(user.uid)}, {new_member.first_name}!")
            try:
                # 解除
                bot.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user_id,
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            except Exception as e:
                logging.error(f'Failed to unrestrict new member: {e}')
        else:
            reply_msg = bot.send_message(message.chat.id,
                                         f"你没有进行用户验证,请私聊本机器人进行验证 @paoluzsc_bot \n验证通过后自动解除，如验证后被踢出可重新入群自动验证! {new_member.first_name}!")
            # 启动线程在5分钟后检查验证状态，未验证则踢出群
            Thread(target=kick_if_not_verified,
                   args=(user_id, new_member.first_name, message.chat.id, reply_msg)).start()


def kick_if_not_verified(user_id, user_name, chat_id, reply_msg):
    time.sleep(180)
    bot.delete_message(chat_id=chat_id, message_id=reply_msg.message_id)
    user = User.get_or_none(user_id=user_id)
    if user and not user.validated:
        logging.warning(f'User {user_name} ({user_id}) did not verify , sleeping...')
        bot.kick_chat_member(chat_id, user_id, until_date=int(time.time()) + 60)
        msg_tg = bot.send_message(chat_id, f"{user_name} 因未验证已被移出群组。")
        time.sleep(60)  # 等待60秒
        bot.delete_message(chat_id=chat_id, message_id=msg_tg.message_id)
        return
    elif user and user.validated:
        bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user.user_id,
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        bot.send_message(chat_id, f"{WELCOME_AUTH_MSG} {mask_uid(user.uid)}, {user_name}!")
    else:
        logging.warning(f"User {user.uid} is not authorized to join the chat")
    return


@bot.message_handler(commands=['scan'])
def handle_scan(message):
    if len(message.text.split()) != 3:
        return
        # bot.reply_to(message, "Usage: /scan <group_id>")

    try:
        passwd = message.text.split()[1]
        if passwd != "114514":
            bot.reply_to(message, "Wrong password")
            return
        group_id = int(message.text.split()[2])
    except ValueError:
        bot.reply_to(message, "Invalid group ID.")
        return

    if message.chat.type != 'private':
        bot.reply_to(message, "请在私聊中进行此操作。")
        return

    current_time = time.time()

    # 查找超过60分钟未验证的用户
    users = User.select().where(
        (User.validated == False) &
        (User.code_generated_time.is_null(False)) &
        ((current_time - User.code_generated_time) > 3600)  # 超过60分钟
    )

    kicked_user_count = 0  # 计数器，记录被踢出的用户数量
    deleted_user_count = 0  # 计数器，记录从数据库中删除的用户数量

    for user in users:
        try:
            # 尝试踢出用户
            bot.kick_chat_member(group_id, user.user_id, until_date=int(time.time()) + 60)  # 踢出用户60秒
            logging.warning(f"Kicked user {user.user_id} from group {group_id}\n")
            kicked_user_count += 1

            # 删除用户记录
            user.delete_instance()
            deleted_user_count += 1
        except Exception as e:
            logging.error(f'Failed to kick user {user.user_id} from group {group_id}: {e}')

    bot.reply_to(message, f"扫描完成，从群 {group_id} 中移出了 {kicked_user_count} 个未验证用户，"
                          f"并从数据库中删除了 {deleted_user_count} 个超过60分钟未验证的用户。")


def start_flask_app():
    app.run(host='0.0.0.0', port=5000)


if __name__ == '__main__':
    Thread(target=start_flask_app).start()
    bot.polling()

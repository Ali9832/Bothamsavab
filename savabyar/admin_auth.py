"""Password verification and expiring administrator sessions."""

import hashlib
import hmac
import logging
import secrets

LOG = logging.getLogger('savabyar')
ITERATIONS = 600_000


def hash_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError('رمز مدیر باید بین ۸ تا ۱۲۸ نویسه باشد.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        rounds = int(iterations)
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= rounds <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), rounds)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False


def password_version(encoded):
    return hashlib.sha256(encoded.encode()).hexdigest()


def authenticated(bot, user_id):
    return bool(bot.is_admin(user_id) and bot.cfg.admin_password_hash
                and bot.store.admin_login_valid(user_id, password_version(bot.cfg.admin_password_hash)))


def prompt_login(bot, user_id, notice=''):
    bot.store.clear_session(user_id)
    bot.store.clear_admin_session(user_id)
    if not bot.cfg.admin_password_hash:
        bot.show_screen(user_id, '🔐 پنل مدیر قفل است.\n\n'
                        'رمز مدیر را در ADMIN_PASSWORD فایل تنظیمات وارد و بات را دوباره اجرا کنید. '
                        'رمز باید بین ۸ تا ۱۲۸ نویسه باشد.')
        return
    bot.store.save_admin_session(user_id, {'step': 'login'})
    remaining = bot.store.admin_lock_remaining(user_id)
    text = ('🔐 ورود به پنل مدیر\n\n' + (notice + '\n\n' if notice else ''))
    if remaining:
        text += f'ورود موقتاً قفل است؛ {(remaining + 59) // 60} دقیقه دیگر دوباره تلاش کنید.'
    else:
        text += 'رمز پنل مدیر را در یک پیام وارد کنید.\nبرای لغو /cancel را بفرستید.'
    bot.show_screen(user_id, text)


def require_login(bot, user_id):
    if authenticated(bot, user_id):
        return True
    prompt_login(bot, user_id)
    return False


def receive_password(bot, message):
    """Called only for a trusted administrator's active private login form."""
    from bot import APIError
    user_id = message['from']['id']
    password = message.get('text') or ''
    if message.get('message_id'):
        try:
            bot.api.call('deleteMessage', request_timeout=5, chat_id=user_id, message_id=message['message_id'])
        except APIError:
            LOG.warning('Could not delete an administrator password message')
    if bot.store.admin_lock_remaining(user_id):
        prompt_login(bot, user_id)
        return False
    if bot.cfg.admin_password_hash and verify_password(password, bot.cfg.admin_password_hash):
        bot.store.grant_admin_login(user_id, password_version(bot.cfg.admin_password_hash),
                                   bot.cfg.admin_session_minutes * 60)
        bot.store.clear_admin_session(user_id)
        return True
    bot.store.fail_admin_login(user_id)
    prompt_login(bot, user_id, notice='رمز صحیح نیست.')
    return False

"""Private administrator controls. Recording an expense never transfers money."""

import secrets

# The official web client's MY_BANK route is "services"; its card-to-card
# entry route is /services/s/card_to_card (checked 2026-11-03).
BALE_CARD_TRANSFER_URL = 'https://web.bale.ai/services/s/card_to_card'


def keyboard(rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row] for row in rows]}


def back_keyboard():
    return keyboard([[('⬅️ بازگشت به پنل مدیر', 'admin:home')]])


def fund_keyboard():
    from bot import INTENTS
    return keyboard([[(INTENTS[key], 'expense:fund:' + key)]
                     for key in ('mahdavi', 'heyat', 'unrestricted')]
                    + [[('⬅️ بازگشت به پنل مدیر', 'admin:home')]])


def panel(bot, chat_id, notice=''):
    from bot import balance_text
    bot.show_screen(chat_id, (notice + '\n\n' if notice else '') + '🔐 پنل مدیر ثواب‌یار\n\n'
             + balance_text(bot.store.balances(is_test=bot.cfg.is_test), is_test=bot.cfg.is_test),
             reply_markup=keyboard([
                 [('📊 تازه‌سازی موجودی', 'admin:balances')],
                 [('🧾 ثبت هزینه انجام‌شده', 'admin:expense')],
                 [('💳 انتقال وجه / کارت‌به‌کارت', 'admin:payout')],
                 [('🔒 خروج از پنل مدیر', 'admin:logout')],
             ]))


def fund_choices(bot, user_id):
    bot.store.clear_session(user_id)
    bot.store.save_admin_session(user_id, {'step': 'fund', 'request_id': secrets.token_hex(12)})
    bot.show_screen(user_id, 'این بخش فقط هزینه‌ای را ثبت می‌کند که انتقال آن را قبلاً خارج از بات انجام داده‌اید؛ '
             'هیچ انتقال وجهی از این فرم انجام نمی‌شود.\n\nهزینه از محل کدام بخش پرداخت شده است؟',
             reply_markup=fund_keyboard())


def description_prompt(bot, user_id, state):
    state['step'] = 'description'
    bot.store.save_admin_session(user_id, state)
    bot.show_screen(user_id, 'عنوان کوتاه هزینه را بنویسید (۳ تا ۱۵۰ نویسه). '
             'این عنوان در کانال منتشر می‌شود؛ نام اشخاص و اطلاعات بانکی ننویسید.\nبرای لغو /cancel را بفرستید.',
             reply_markup=back_keyboard())


def handle_callback(bot, query):
    from bot import INTENTS
    user = query.get('from') or {}
    user_id = user.get('id')
    chat = (query.get('message') or {}).get('chat') or {}
    private = chat.get('type') == 'private' and chat.get('id') == user_id
    if private:
        bot.bind_admin(user)
    allowed = private and bot.is_admin(user_id)
    bot.answer_button(query['id'], 'پنل مدیر' if allowed else 'این بخش فقط برای مدیر صندوق است.')
    if not allowed:
        return
    bot.track_screen(user_id, query.get('message') or {})
    data = query.get('data', '')
    from admin_auth import prompt_login, require_login
    if data == 'admin:logout':
        bot.store.revoke_admin_login(user_id)
        prompt_login(bot, user_id, notice='از پنل مدیر خارج شدید.')
        return
    if not require_login(bot, user_id):
        return
    if data in ('admin:balances', 'admin:home'):
        bot.store.clear_admin_session(user_id)
        panel(bot, user_id)
        return
    if data == 'admin:payout':
        bot.show_screen(user_id, '💳 انتقال وجه با بله\n\n'
                 'با دکمه زیر، صفحه کارت‌به‌کارت نسخه وب بله را باز کنید. ممکن است لازم باشد وارد حساب بله خود شوید. '
                 'کارت مبدأ، کارت مقصد و مبلغ را در همان محیط رسمی بله وارد کنید.\n\n'
                 'بعد از انتقال موفق، به این گفتگو برگردید و «انتقال انجام شد؛ ثبت هزینه» را بزنید '
                 'تا بخش هزینه، مبلغ، توضیح و شماره پیگیری را ثبت کنید.\n\n'
                 'بازکردن این لینک به معنی انتقال یا ثبت هزینه نیست؛ نتیجه کارت‌به‌کارت خودکار به بات برنمی‌گردد.',
                 reply_markup={'inline_keyboard': [
                     [{'text': '💳 بازکردن کارت‌به‌کارت بله (وب)', 'url': BALE_CARD_TRANSFER_URL}],
                     [{'text': '🧾 انتقال انجام شد؛ ثبت هزینه', 'callback_data': 'admin:expense'}],
                     [{'text': '⬅️ بازگشت به پنل مدیر', 'callback_data': 'admin:home'}],
                 ]})
        return
    if data == 'admin:expense':
        fund_choices(bot, user_id)
        return
    state = bot.store.admin_session(user_id)
    if not state:
        panel(bot, user_id, notice='فرم فعال نیست یا قبلاً ثبت شده است.')
        return
    if data.startswith('expense:fund:') and state['step'] == 'fund':
        fund = data.split(':')[-1]
        if fund not in INTENTS:
            return
        available = bot.store.balances(is_test=bot.cfg.is_test)[fund]['balance_rial'] // 10
        if available <= 0:
            bot.show_screen(user_id, 'این بخش مانده قابل مصرف ندارد؛ بخش دیگری را انتخاب کنید یا /cancel را بفرستید.',
                            reply_markup=fund_keyboard())
            return
        state.update(step='amount', fund=fund)
        bot.store.save_admin_session(user_id, state)
        bot.show_screen(user_id, f'مبلغ هزینه را به تومان وارد کنید.\nمانده این بخش: {available:,} تومان.\nبرای لغو /cancel را بفرستید.',
                        reply_markup=back_keyboard())
    elif data.startswith('expense:purpose:') and state['step'] == 'purpose':
        purpose = data.split(':')[-1]
        if purpose in ('mahdavi', 'heyat'):
            state['purpose'] = purpose
            description_prompt(bot, user_id, state)
    elif data == 'expense:confirm:' + state['request_id'] and state['step'] == 'confirm':
        try:
            expense = bot.store.record_expense(
                request_id=state['request_id'], admin_id=user_id, fund=state['fund'],
                purpose=state['purpose'], amount_rial=state['amount_toman'] * 10,
                description=state['description'], transfer_reference=state['reference'],
                is_test=bot.cfg.is_test, channel_id=bot.cfg.channel)
        except ValueError as exc:
            bot.show_screen(user_id, str(exc) + '\nبرای لغو /cancel را بفرستید.', reply_markup=back_keyboard())
            return
        panel(bot, user_id, notice=f"هزینه ثبت شد؛ شماره گزارش EX-{expense['id'].upper()}.\n"
              'مانده بخش مربوط کم شد و گزارش برای انتشار در کانال در صف قرار گرفت. '
              'انتقال جدیدی توسط بات انجام نشد.')


def handle_message(bot, message):
    from bot import INTENTS, parse_toman
    from admin_auth import authenticated, prompt_login, receive_password, require_login
    user_id = message['from']['id']
    text = str(message.get('text') or '').strip()
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
    if command == '/id':
        bot.show_screen(user_id, f'شناسه عددی حساب شما: {user_id}')
        return True
    if command in ('/admin', '/expense', '/logout'):
        if not bot.is_admin(user_id):
            bot.show_screen(user_id, 'این بخش فقط برای مدیر صندوق است.')
            return True
        if command == '/logout':
            bot.store.revoke_admin_login(user_id)
            prompt_login(bot, user_id, notice='از پنل مدیر خارج شدید.')
            return True
        if not require_login(bot, user_id):
            return True
        bot.store.clear_admin_session(user_id)
        bot.store.clear_session(user_id)
        if command == '/expense':
            fund_choices(bot, user_id)
        else:
            panel(bot, user_id)
        return True
    state = bot.store.admin_session(user_id) if bot.is_admin(user_id) else None
    if not state:
        return False
    if state['step'] == 'login':
        if receive_password(bot, message):
            panel(bot, user_id, notice='ورود موفق بود.')
        return True
    if not authenticated(bot, user_id):
        prompt_login(bot, user_id, notice='نشست شما تمام شده است؛ دوباره وارد شوید.')
        return True
    if state['step'] == 'amount':
        available = bot.store.balances(is_test=bot.cfg.is_test)[state['fund']]['balance_rial'] // 10
        try:
            state['amount_toman'] = parse_toman(text, 1, available)
        except ValueError:
            bot.show_screen(user_id, f'عدد صحیح تومان وارد کنید؛ مانده این بخش {available:,} تومان است.',
                            reply_markup=back_keyboard())
            return True
        if state['fund'] == 'unrestricted':
            state['step'] = 'purpose'
            bot.store.save_admin_session(user_id, state)
            bot.show_screen(user_id, 'این هزینه از محل مشارکت بدون نیت، برای کدام زمینه مصرف شده است؟',
                     reply_markup=keyboard([[(INTENTS[key], 'expense:purpose:' + key)] for key in ('mahdavi', 'heyat')]
                                           + [[('⬅️ بازگشت به پنل مدیر', 'admin:home')]]))
        else:
            state['purpose'] = state['fund']
            description_prompt(bot, user_id, state)
    elif state['step'] == 'description':
        if not 3 <= len(text) <= 150 or any(ord(c) < 32 for c in text):
            bot.show_screen(user_id, 'عنوان را در یک خط و بین ۳ تا ۱۵۰ نویسه بنویسید.', reply_markup=back_keyboard())
            return True
        state.update(description=text, step='reference')
        bot.store.save_admin_session(user_id, state)
        bot.show_screen(user_id, 'شماره پیگیری انتقال انجام‌شده را وارد کنید (۳ تا ۸۰ نویسه). '
                 'این شماره فقط در سوابق مدیر نگهداری می‌شود و در کانال منتشر نمی‌شود.', reply_markup=back_keyboard())
    elif state['step'] == 'reference':
        if not 3 <= len(text) <= 80 or any(ord(c) < 32 for c in text):
            bot.show_screen(user_id, 'شماره پیگیری معتبر را در یک خط، بین ۳ تا ۸۰ نویسه بنویسید.', reply_markup=back_keyboard())
            return True
        state.update(reference=text, step='confirm')
        bot.store.save_admin_session(user_id, state)
        bot.show_screen(user_id, f"بخش: {INTENTS[state['fund']]}\nمحل مصرف: {INTENTS[state['purpose']]}\n"
                 f"مبلغ: {state['amount_toman']:,} تومان\nبابت: {state['description']}\n"
                 f"شماره پیگیری خصوصی: {state['reference']}\n\n"
                 'فقط اگر انتقال قبلاً انجام شده، ثبت را تأیید کنید. با تأیید، مانده کم می‌شود و گزارش در کانال منتشر خواهد شد.',
                 reply_markup=keyboard([[('ثبت هزینه انجام‌شده', 'expense:confirm:' + state['request_id'])],
                                        [('⬅️ بازگشت به پنل مدیر', 'admin:home')]]))
    else:
        bot.show_screen(user_id, 'برای ادامه از دکمه‌های همین فرم استفاده کنید؛ برای لغو /cancel را بفرستید.',
                        reply_markup=back_keyboard())
    return True

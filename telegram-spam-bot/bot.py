# -*- coding: utf-8 -*-
"""
bot.py — сам бот-антиспам для комментариев под постами Telegram-канала.

Как это работает в двух словах:
  1) Под постами канала включены комментарии — это отдельная «группа
     обсуждений», привязанная к каналу.
  2) Бот добавлен в эту группу администратором с правами «удалять
     сообщения» и «блокировать пользователей».
  3) Каждое новое сообщение бот прогоняет через фильтр (spam_filter.py):
       - явный спам        -> удалить сообщение + забанить автора;
       - подозрительно     -> удалить сообщение + прислать вам кнопки;
       - обычный человек   -> не трогаем, копим ему «доверие».

Запуск:  python bot.py
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from spam_filter import analyze
from storage import Storage

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("antispam")

db = Storage(config.DB_PATH)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip()


async def _is_admin(context, chat_id: int, user_id: int) -> bool:
    """Проверяем, не админ ли автор — админов никогда не трогаем."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _notify_admin(context, text: str, keyboard=None) -> None:
    """Шлём уведомление вам в личку (если задан ADMIN_CHAT_ID)."""
    if not config.ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=int(config.ADMIN_CHAT_ID),
            text=text,
            reply_markup=keyboard,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Не смог отправить уведомление админу: %s", e)


# ---------------------------------------------------------------------------
# Команды в личке с ботом
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        "👋 Привет! Я бот-антиспам для комментариев канала.\n\n"
        f"🆔 Ваш chat_id: <code>{chat.id}</code>\n"
        "Скопируйте это число в настройку <b>ADMIN_CHAT_ID</b> — тогда я буду "
        "присылать вам сюда подозрительные сообщения с кнопками.\n\n"
        "Дальше добавьте меня в группу обсуждений канала и дайте права "
        "администратора (удаление сообщений + блокировка). Подробности — в README.",
        parse_mode=constants.ParseMode.HTML,
    )
    log.info("/start от user_id=%s chat_id=%s", user.id, chat.id)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(
        f"chat_id: <code>{chat.id}</code>", parse_mode=constants.ParseMode.HTML
    )


# ---------------------------------------------------------------------------
# Главный обработчик сообщений в группе обсуждений
# ---------------------------------------------------------------------------
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    # Игнорируем служебные штуки и сам пост канала, который автоматически
    # прилетает в обсуждения.
    if getattr(msg, "is_automatic_forward", False):
        return
    # Сообщения «от имени канала» (sender_chat) — это не обычный участник.
    if msg.sender_chat is not None:
        return

    user = msg.from_user
    if user is None or user.is_bot:
        return

    chat_id = msg.chat_id
    user_id = user.id

    # 1) Админов и доверенных/белый список — пропускаем без проверки.
    if db.is_whitelisted(user_id):
        return
    if db.clean_count(user_id) >= config.TRUST_AFTER_MESSAGES:
        return
    if await _is_admin(context, chat_id, user_id):
        return

    text = msg.text or msg.caption or ""
    is_new_user = db.clean_count(user_id) == 0

    verdict = analyze(
        text,
        is_new_user=is_new_user,
        is_forward=bool(msg.forward_origin),
        has_photo=bool(msg.photo or msg.video or msg.animation),
        display_name=_display_name(user),
        has_username=bool(user.username),
    )

    log.info(
        "msg от %s (@%s): score=%s action=%s | %s",
        user_id, user.username, verdict.score, verdict.action,
        " ".join(verdict.reasons) or "чисто",
    )

    # 2) Обычный человек — запоминаем «чистое» сообщение (растёт доверие).
    if verdict.action == "ok":
        db.add_clean_message(user_id)
        return

    who = _display_name(user) or "без имени"
    uname = f"@{user.username}" if user.username else "без username"
    preview = (text[:400] + "…") if len(text) > 400 else text
    reasons = "\n".join(verdict.reasons)

    # 3) Явный спам -> удалить + забанить, уведомить с кнопкой «Разбанить».
    if verdict.action == "ban":
        deleted = await _safe_delete(context, chat_id, msg.message_id)
        banned = await _safe_ban(context, chat_id, user_id)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "↩️ Разбанить (это был не спам)",
                callback_data=f"unban:{chat_id}:{user_id}",
            )
        ]])
        await _notify_admin(
            context,
            f"🚫 <b>Заблокирован спам</b> (score {verdict.score})\n"
            f"Автор: {who} ({uname}, id <code>{user_id}</code>)\n"
            f"Удалено: {'да' if deleted else 'нет'} | Бан: {'да' if banned else 'нет'}\n\n"
            f"<b>Причины:</b>\n{reasons}\n\n"
            f"<b>Текст:</b>\n<code>{_esc(preview)}</code>",
            kb,
        )
        return

    # 4) Подозрительно -> удалить (если включено) + спросить вас.
    deleted = False
    if config.DELETE_ON_SUSPICION:
        deleted = await _safe_delete(context, chat_id, msg.message_id)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Забанить", callback_data=f"ban:{chat_id}:{user_id}"),
        InlineKeyboardButton("✅ Не спам", callback_data=f"safe:{chat_id}:{user_id}"),
    ]])
    await _notify_admin(
        context,
        f"⚠️ <b>Подозрительное сообщение</b> (score {verdict.score})\n"
        f"Автор: {who} ({uname}, id <code>{user_id}</code>)\n"
        f"Сообщение {'удалено' if deleted else 'оставлено'}.\n\n"
        f"<b>Причины:</b>\n{reasons}\n\n"
        f"<b>Текст:</b>\n<code>{_esc(preview)}</code>",
        kb,
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _safe_delete(context, chat_id: int, message_id: int) -> bool:
    try:
        await context.bot.delete_message(chat_id, message_id)
        return True
    except Exception as e:
        log.warning("Не удалось удалить сообщение: %s", e)
        return False


async def _safe_ban(context, chat_id: int, user_id: int) -> bool:
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        return True
    except Exception as e:
        log.warning("Не удалось забанить: %s", e)
        return False


# ---------------------------------------------------------------------------
# Кнопки под уведомлениями
# ---------------------------------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        action, chat_id, user_id = query.data.split(":")
        chat_id, user_id = int(chat_id), int(user_id)
    except ValueError:
        return

    if action == "ban":
        ok = await _safe_ban(context, chat_id, user_id)
        note = "🚫 Забанен." if ok else "Не смог забанить (проверьте права бота)."
    elif action == "unban":
        try:
            await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            db.whitelist(user_id)
            note = "↩️ Разбанен и добавлен в белый список."
        except Exception as e:
            note = f"Не смог разбанить: {e}"
    elif action == "safe":
        db.whitelist(user_id)
        note = "✅ Добавлен в белый список — больше проверять не буду."
    else:
        note = "Неизвестная кнопка."

    # Дописываем результат под уведомлением, чтобы было видно, что сделано.
    try:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n<b>➡️ {note}</b>",
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception:
        await query.message.reply_text(note)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main() -> None:
    config.validate()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))

    # Слушаем сообщения только в группах/супергруппах (обсуждения канала).
    group_filter = (filters.ChatType.GROUPS) & (~filters.StatusUpdate.ALL)
    app.add_handler(MessageHandler(group_filter, on_group_message))

    log.info("Бот запущен. Жду сообщения…  (Ctrl+C чтобы остановить)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

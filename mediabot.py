import asyncio
import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from people import PEOPLE_RAW

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
NOTIFY_HOUR = int(os.environ.get("NOTIFY_HOUR", "12"))
WEEKS = 4  # текущая неделя + 3 следующих
HALLS = ["Европа", "Берлин"]

WELCOME = (
    "Привет! Это информационный бот Расписание | REC MEDIA в котором можно смотреть наше расписание.\n\n"
    "Снизу вы найдете кнопки, по которым можно получить всю необходимую информацию.\n\n"
    "Спасибо, что служите вместе с нами!"
)
MAIN_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Расписание", callback_data="menu:schedule")]]
)

# key: (подпись, тип)  тип: person / people / hall / text
FIELDS = {
    "director": ("Режиссер", "person"),
    "operator": ("Оператор", "person"),
    "stab": ("Стаб", "person"),
    "clicker": ("Кликер", "person"),
    "sound": ("Звук", "person"),
    "tech": ("Группа тех. сбора", "people"),
    "hall": ("Зал", "hall"),
    "speaker": ("Спикер", "text"),
    "host": ("Ведущий", "text"),
    "worship": ("Состав воршип", "text"),
}
# роли, по которым приходят напоминания
NOTIFY_ROLES = {
    "director": "Режиссер (трансляция)",
    "operator": "Оператор (трансляция)",
    "stab": "Стаб (трансляция)",
    "clicker": "Кликер",
    "sound": "Звук",
    "tech": "Группа технического сбора оборудования",
}
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


# ---------------------------------------------------------------- База людей
@dataclass
class Person:
    name: str
    username: str
    uid: int | None


def load_people() -> dict[str, Person]:
    people: dict[str, Person] = {}
    for line in PEOPLE_RAW.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        uid = int(parts.pop()) if len(parts) > 2 and parts[-1].isdigit() else None
        username = parts.pop().lstrip("@")
        name = " ".join(parts)
        if name and username:
            people[username.lower()] = Person(name, username, uid)
    return people


PEOPLE = load_people()


def norm(u: str) -> str:
    return u.strip().lstrip("@").lower()


def split_users(value: str | None) -> list[str]:
    return [norm(x) for x in re.split(r"[,\s]+", value or "") if norm(x)]


def person_html(u: str) -> str:
    p = PEOPLE.get(u)
    if p:
        return f'<a href="https://t.me/{p.username}">{html.escape(p.name)}</a>'
    return html.escape("@" + u)


def people_html(value: str | None, sep: str = ", ") -> str:
    return sep.join(person_html(u) for u in split_users(value))


def person_plain(u: str) -> str:
    p = PEOPLE.get(u)
    return p.name if p else "@" + u


# ---------------------------------------------------------------- БД
db = sqlite3.connect("schedule.db", check_same_thread=False)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, chat_id INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS event_fields (
        date TEXT, key TEXT, value TEXT, PRIMARY KEY (date, key));
    """
)


def register_user(username: str | None, chat_id: int):
    if username:
        db.execute("INSERT OR REPLACE INTO users VALUES (?, ?)", (username.lower(), chat_id))
        db.commit()


def resolve_chat(username: str) -> int | None:
    p = PEOPLE.get(username)
    if p and p.uid:
        return p.uid
    row = db.execute("SELECT chat_id FROM users WHERE username = ?", (username,)).fetchone()
    return row[0] if row else None


def get_event(d: date) -> dict:
    rows = db.execute("SELECT key, value FROM event_fields WHERE date = ?", (d.isoformat(),)).fetchall()
    return dict(rows)


def set_field(d: date, key: str, value: str):
    if value:
        db.execute("INSERT OR REPLACE INTO event_fields VALUES (?, ?, ?)", (d.isoformat(), key, value))
    else:
        db.execute("DELETE FROM event_fields WHERE date = ? AND key = ?", (d.isoformat(), key))
    db.commit()


# ---------------------------------------------------------------- Утилиты
def today() -> date:
    return datetime.now(TZ).date()


def sundays() -> list[date]:
    t = today()
    first = t + timedelta(days=(6 - t.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(WEEKS)]


def render(d: date, full: bool = True) -> str:
    f = get_event(d)

    def txt(key, empty):
        return html.escape(f.get(key) or empty)

    # Добавляем аргумент sep со значением по умолчанию (запятая и пробел)
    def ppl(key, empty="Не назначен", sep=", "):
        return people_html(f.get(key), sep=sep) or empty

    lines = [
        f"<b>{WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}</b>",
        f"Зал: <b>{txt('hall', 'Не выбран')}</b>",
        "",
        "<b>Трансляция</b>",
        f"<blockquote>Режиссер: {ppl('director')}\nОператор: {ppl('operator')}\nСтаб: {ppl('stab')}</blockquote>",
        "<b>Кликер</b>",
        f"<blockquote>{ppl('clicker')}</blockquote>",
        "<b>Звук</b>",
        f"<blockquote>{ppl('sound')}</blockquote>",
    ]
    if full:
        # 1. Выносим вызов в переменную
        # 2. Используем СТРОГО ОДИН слеш: sep='\n'
        tech_people = ppl('tech', 'Не назначены', sep='\n')
            
        lines += [
            "<b>Группа технического сбора оборудования</b>",
            f"<blockquote>{tech_people}</blockquote>",
            "<b>Состав воршип</b>",
            f"<blockquote>{txt('worship', '-')}</blockquote>",
            f"Спикер: {txt('speaker', '-')}",
            f"Ведущий: {txt('host', '-')}",
        ]
    return "\n".join(lines)

def week_kb(offset: int, is_admin: bool) -> InlineKeyboardMarkup:
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"w:{offset - 1}"))
    if offset < WEEKS - 1:
        nav.append(InlineKeyboardButton(text="Далее", callback_data=f"w:{offset + 1}"))
    rows = [nav] if nav else []
    if is_admin:
        rows.append([InlineKeyboardButton(text="Редактировать", callback_data=f"e:{offset}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_kb(offset: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"f:{offset}:{key}")
        for key, (label, _) in FIELDS.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Назад к расписанию", callback_data=f"w:{offset}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def people_buttons(callback_for, marked: set[str] | None = None) -> list[list[InlineKeyboardButton]]:
    buttons = []
    for u, p in PEOPLE.items():
        label = p.name
        if marked is not None:
            label = ("[x] " if u in marked else "[ ] ") + label
        buttons.append(InlineKeyboardButton(text=label, callback_data=callback_for(u)))
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def pick_kb(offset: int, key: str) -> InlineKeyboardMarkup:
    rows = people_buttons(lambda u: f"p:{offset}:{key}:{u}")
    rows.append(
        [
            InlineKeyboardButton(text="Очистить", callback_data=f"pc:{offset}:{key}"),
            InlineKeyboardButton(text="Назад", callback_data=f"e:{offset}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tech_kb(offset: int) -> InlineKeyboardMarkup:
    selected = set(split_users(get_event(sundays()[offset]).get("tech")))
    rows = people_buttons(lambda u: f"t:{offset}:{u}", marked=selected)
    rows.append(
        [
            InlineKeyboardButton(text="Очистить", callback_data=f"pc:{offset}:tech"),
            InlineKeyboardButton(text="Готово", callback_data=f"w:{offset}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


# ---------------------------------------------------------------- Хендлеры
router = Router()


class Edit(StatesGroup):
    value = State()


async def show_schedule(m: Message):
    register_user(m.from_user.username, m.chat.id)
    await m.answer(render(sundays()[0]), reply_markup=week_kb(0, m.from_user.id in ADMIN_IDS))


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    register_user(m.from_user.username, m.chat.id)
    # убираем старую клавиатуру внизу экрана, если она осталась с прошлой версии
    tmp = await m.answer(".", reply_markup=ReplyKeyboardRemove())
    await tmp.delete()
    await m.answer(WELCOME, reply_markup=MAIN_KB)


@router.callback_query(F.data == "menu:schedule")
async def cb_menu_schedule(c: CallbackQuery, state: FSMContext):
    await state.clear()
    register_user(c.from_user.username, c.message.chat.id)
    await c.message.answer(render(sundays()[0]), reply_markup=week_kb(0, c.from_user.id in ADMIN_IDS))
    await c.answer()


@router.message(Command("schedule"))
@router.message(F.text == "Расписание")
async def cmd_schedule(m: Message, state: FSMContext):
    await state.clear()
    await show_schedule(m)


@router.callback_query(F.data.startswith("w:"))
async def cb_week(c: CallbackQuery, state: FSMContext):
    await state.clear()
    offset = int(c.data.split(":")[1])
    await safe_edit(c.message, render(sundays()[offset]), week_kb(offset, c.from_user.id in ADMIN_IDS))
    await c.answer()


@router.callback_query(F.data.startswith("e:"))
async def cb_edit(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    offset = int(c.data.split(":")[1])
    await safe_edit(c.message, render(sundays()[offset]) + "\n\n<i>Что изменить?</i>", edit_kb(offset))
    await c.answer()


@router.callback_query(F.data.startswith("f:"))
async def cb_field(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    _, offset, key = c.data.split(":")
    offset = int(offset)
    label, kind = FIELDS[key]
    d = sundays()[offset]
    head = f"<b>{WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}</b>\n{label}"

    if kind in ("person", "people") and not PEOPLE:
        return await c.answer("Список людей пуст. Добавь их в people.py", show_alert=True)

    if kind == "person":
        current = people_html(get_event(d).get(key)) or "не назначен"
        await safe_edit(c.message, f"{head}\nСейчас: {current}\n\nВыбери человека:", pick_kb(offset, key))
    elif kind == "people":
        await safe_edit(c.message, f"{head}\n\nОтмечай людей, затем нажми Готово:", tech_kb(offset))
    elif kind == "hall":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=h, callback_data=f"h:{offset}:{i}") for i, h in enumerate(HALLS)],
                [
                    InlineKeyboardButton(text="Сбросить", callback_data=f"h:{offset}:-1"),
                    InlineKeyboardButton(text="Назад", callback_data=f"e:{offset}"),
                ],
            ]
        )
        await safe_edit(c.message, f"{head}\n\nВыбери зал:", kb)
    else:
        await state.set_state(Edit.value)
        await state.update_data(offset=offset, key=key)
        await c.message.answer(f"<b>{label}</b>\nОтправь текст (имя или список).\nОтправь «-», чтобы очистить поле.")
    await c.answer()


@router.callback_query(F.data.startswith("p:"))
async def cb_pick(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    _, offset, key, username = c.data.split(":")
    offset = int(offset)
    set_field(sundays()[offset], key, username)
    await safe_edit(c.message, render(sundays()[offset]), week_kb(offset, True))
    await c.answer("Сохранено")


@router.callback_query(F.data.startswith("pc:"))
async def cb_pick_clear(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    _, offset, key = c.data.split(":")
    offset = int(offset)
    set_field(sundays()[offset], key, "")
    await safe_edit(c.message, render(sundays()[offset]), week_kb(offset, True))
    await c.answer("Очищено")


@router.callback_query(F.data.startswith("t:"))
async def cb_tech_toggle(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    _, offset, username = c.data.split(":")
    offset = int(offset)
    d = sundays()[offset]
    users = split_users(get_event(d).get("tech"))
    if username in users:
        users.remove(username)
    else:
        users.append(username)
    set_field(d, "tech", ", ".join(users))
    try:
        await c.message.edit_reply_markup(reply_markup=tech_kb(offset))
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("h:"))
async def cb_hall(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    _, offset, idx = c.data.split(":")
    offset, idx = int(offset), int(idx)
    set_field(sundays()[offset], "hall", HALLS[idx] if idx >= 0 else "")
    await safe_edit(c.message, render(sundays()[offset]), week_kb(offset, True))
    await c.answer("Сохранено")


@router.message(Edit.value)
async def got_value(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return await state.clear()
    data = await state.get_data()
    offset, key = data["offset"], data["key"]
    text = (m.text or "").strip()
    value = "" if text in ("-", "—") else text
    d = sundays()[offset]
    set_field(d, key, value)
    await state.clear()
    await m.answer(render(d), reply_markup=week_kb(offset, True))


@router.message(Command("remind"))
async def cmd_remind(m: Message, bot: Bot):
    if m.from_user.id not in ADMIN_IDS:
        return
    await send_reminders(bot)
    await m.answer("Рассылка выполнена.")


# ---------------------------------------------------------------- Очистка данных (только админы)
def confirm_kb(yes_data: str, yes_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes_text, callback_data=yes_data),
                InlineKeyboardButton(text="Отмена", callback_data="cx"),
            ]
        ]
    )


@router.message(Command("clear_week"))
async def cmd_clear_week(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return await m.answer("Эта команда доступна только админам.")
    rows = [
        [InlineKeyboardButton(text=f"{d:%d.%m.%Y}", callback_data=f"cw:{d.isoformat()}")]
        for d in sundays()
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cx")])
    await m.answer(
        "Данные какого воскресенья очистить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("cw:"))
async def cb_clear_week_ask(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    d = date.fromisoformat(c.data.split(":", 1)[1])
    filled = len(get_event(d))
    await safe_edit(
        c.message,
        f"Очистить все данные на <b>{d:%d.%m.%Y}</b>?\nЗаполненных полей: {filled}.\nЭто действие нельзя отменить.",
        confirm_kb(f"cwy:{d.isoformat()}", "Да, очистить"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("cwy:"))
async def cb_clear_week_do(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    d = date.fromisoformat(c.data.split(":", 1)[1])
    db.execute("DELETE FROM event_fields WHERE date = ?", (d.isoformat(),))
    db.commit()
    await c.message.edit_text(f"Данные на {d:%d.%m.%Y} очищены.")
    await c.answer()


@router.message(Command("clear_all"))
async def cmd_clear_all(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return await m.answer("Эта команда доступна только админам.")
    dates = db.execute("SELECT COUNT(DISTINCT date) FROM event_fields").fetchone()[0]
    await m.answer(
        f"Удалить данные по <b>всем</b> датам?\nДат с данными: {dates}.\n"
        "Это действие нельзя отменить.",
        reply_markup=confirm_kb("cay", "Да, удалить все"),
    )


@router.callback_query(F.data == "cay")
async def cb_clear_all_do(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Только для админов", show_alert=True)
    db.execute("DELETE FROM event_fields")
    db.commit()
    await c.message.edit_text("Данные по всем датам удалены.")
    await c.answer()


@router.callback_query(F.data == "cx")
async def cb_cancel(c: CallbackQuery):
    await c.message.edit_text("Отменено.")
    await c.answer()


# ---------------------------------------------------------------- Напоминания
def roles_for(d: date, username: str) -> tuple[list[str], bool]:
    f = get_event(d)
    labels, tech = [], False
    for key, label in NOTIFY_ROLES.items():
        if username in split_users(f.get(key)):
            labels.append(label)
            if key == "tech":
                tech = True
    return labels, tech


def reminder_text(d: date, username: str, full: bool) -> str:
    labels, tech = roles_for(d, username)
    if not labels:
        head = "<b>Напоминание о служении</b>\n\nВ расписании ты больше не указан на эту дату."
    else:
        extra = " (приоритет у группы технического сбора оборудования)" if tech and len(labels) > 1 else ""
        roles = "\n".join(f"• {r}" for r in labels)
        head = (
            f"<b>Напоминание о служении</b>\n\n"
            f"Твоя позиция:\n{roles}\n\n"
            f"Нужно прийти к <b>{'9:00' if tech else '10:00'}</b>{extra}"
        )
    return head + "\n\n" + render(d, full)


def reminder_kb(d: date, username: str, full: bool) -> InlineKeyboardMarkup:
    text = "Свернуть" if full else "Развернуть"
    flag = 0 if full else 1
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=f"x:{d.isoformat()}:{username}:{flag}")]]
    )


@router.callback_query(F.data.startswith("x:"))
async def cb_expand(c: CallbackQuery):
    _, iso, username, flag = c.data.split(":")
    d, full = date.fromisoformat(iso), flag == "1"
    await safe_edit(c.message, reminder_text(d, username, full), reminder_kb(d, username, full))
    await c.answer()


async def send_reminders(bot: Bot):
    undelivered: set[str] = set()
    for days_before in (2, 1):
        d = today() + timedelta(days=days_before)
        f = get_event(d)
        if not f:
            continue
        usernames = {u for key in NOTIFY_ROLES for u in split_users(f.get(key))}
        for u in usernames:
            chat_id = resolve_chat(u)
            if not chat_id:
                undelivered.add(person_plain(u))
                continue
            try:
                await bot.send_message(chat_id, reminder_text(d, u, False), reply_markup=reminder_kb(d, u, False))
            except Exception as exc:  # человек мог не запускать или заблокировать бота
                logging.warning("Не доставлено %s: %s", u, exc)
                undelivered.add(person_plain(u))

    if undelivered:
        msg = (
            "Не удалось отправить напоминание: "
            + ", ".join(sorted(undelivered))
            + "\nПусть нажмут /start у бота."
        )
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin, msg)
            except Exception:
                pass


# ---------------------------------------------------------------- Запуск
async def main():
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(send_reminders, CronTrigger(hour=NOTIFY_HOUR, minute=0, timezone=TZ), args=[bot])
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
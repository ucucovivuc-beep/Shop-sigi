import asyncio
import logging
import os

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from datetime import datetime, timedelta, timezone
import re
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from dotenv import load_dotenv

import db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# Токен Crypto Pay (@CryptoBot → Crypto Pay → Create App). Пусто — оплата через Crypto Bot скрыта
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_API = "https://pay.crypt.bot/api"  # для тестов: https://testnet-pay.crypt.bot/api

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop_bot")

router = Router()

CART_BTN = "🛒 Корзина"
PROFILE_BTN = "👤 Профиль"
ORDER_BTN = "✅ Оформить заявку"
CLEAR_BTN = "🗑 Очистить"
BACK_BTN = "⬅️ Назад"
HOME_BTN = "🏠 Главная"
CANCEL_BTN = "❌ Отмена"
ADD_PRODUCT_BTN = "➕ Добавить товар"
EDIT_PRICE_BTN = "✏️ Изменить цену"
ADD_BANNER_BTN = "🖼 Добавить баннер"
REQUISITES_BTN = "💳 Реквизиты"
BROADCAST_BTN = "📣 Рассылка"
ADMINS_BTN = "👥 Админы"
ADD_ADMIN_BTN = "➕ Добавить админа"
SEND_ALL_BTN = "📤 Отправить всем"
MENU_ADMIN_BTN = "🧭 Главное меню"
MENU_EDIT_TEXT_BTN = "✏️ Изменить текст"
DEFAULT_MENU_TEXT = "🏙 Выберите город:"
STATS_BTN = "📊 Статистика"
REF_ADMIN_BTN = "🤝 Рефералы"
STAT_PERIODS = {"today": ("Сегодня", 0), "week": ("7 дней", 7),
                "month": ("30 дней", 30), "all": ("Всё время", None)}
SAVE_BTN = "✅ Сохранить"

PAY_RUB_BTN = "💳 Рубли (переводом)"
PAY_CRYPTO_BTN = "🤖 Crypto Bot"
PAY_TON_BTN = "💎 TON Keeper (переводом)"
PAY_BALANCE_BTN = "💰 С баланса"
CUSTOMER_PROMO_BTN = "🎟 Ввести промокод"
ADMIN_PROMO_BTN = "🎟 Промокоды"
PROMO_NEW_BTN = "➕ Новый промокод"
KIND_PERCENT_BTN = "% Процент"
KIND_FIXED_BTN = "₽ Фиксированная сумма"
PAY_METHODS = {
    PAY_RUB_BTN: "rub",
    PAY_CRYPTO_BTN: "crypto",
    PAY_TON_BTN: "ton",
    PAY_BALANCE_BTN: "balance",
}
TOPUP_METHODS = {
    PAY_RUB_BTN: "rub",
    PAY_CRYPTO_BTN: "crypto",
    PAY_TON_BTN: "ton",
}
PAY_LABELS = {
    "rub": "Рубли (переводом)",
    "crypto": "Crypto Bot",
    "ton": "TON Keeper (переводом)",
    "balance": "С баланса",
}

# Ключи настроек в таблице settings
REQ_RUB_KEY = "requisites_rub"
REQ_TON_KEY = "requisites_ton"
REQ_RUB_BTN = "💳 Реквизиты рублей"
REQ_TON_BTN = "💎 Реквизиты TON"
REQ_KEYS = {REQ_RUB_BTN: REQ_RUB_KEY, REQ_TON_BTN: REQ_TON_KEY}

# user_id -> {"region": id|None, "category": id|None, "products": {title: product_id}}
nav: dict[int, dict] = {}
# user_id -> {product_id: qty}
carts: dict[int, dict[int, int]] = {}


class AddProduct(StatesGroup):
    region = State()
    category = State()
    name = State()
    description = State()
    price = State()
    delivery = State()
    photo = State()
    confirm = State()


class EditPrice(StatesGroup):
    region = State()
    category = State()
    product = State()
    price = State()


class AddBanner(StatesGroup):
    photo = State()
    caption = State()


class Checkout(StatesGroup):
    method = State()
    promo = State()


class PromoAdd(StatesGroup):
    code = State()
    kind = State()
    value = State()
    max_uses = State()
    expiry = State()


class TopUp(StatesGroup):
    amount = State()
    method = State()


class MenuEdit(StatesGroup):
    text = State()


class RefPct(StatesGroup):
    value = State()


class EditRequisites(StatesGroup):
    method = State()
    text = State()


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class AdminMgmt(StatesGroup):
    add_id = State()


def reply_kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for row in rows:
        b.row(*[KeyboardButton(text=t) for t in row])
    return b.as_markup(resize_keyboard=True)


def nav_state(user_id: int) -> dict:
    return nav.setdefault(user_id, {"region": None, "category": None, "products": {}})


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_db_admin(user_id)


def chunk(items: list[str], size: int = 2) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def regions_menu(user_id: int) -> ReplyKeyboardMarkup:
    st = nav_state(user_id)
    st.update(region=None, category=None, products={})
    names = [f"🏙 {r['name']}" for r in db.get_regions()]
    rows = chunk(names) + [[CART_BTN, PROFILE_BTN]]
    if is_admin(user_id):
        rows.append([ADD_PRODUCT_BTN, EDIT_PRICE_BTN])
        rows.append([ADD_BANNER_BTN, REQUISITES_BTN])
        rows.append([BROADCAST_BTN, ADMIN_PROMO_BTN])
        rows.append([MENU_ADMIN_BTN])
        rows.append([STATS_BTN, REF_ADMIN_BTN])
    if user_id == ADMIN_ID:
        rows.append([ADMINS_BTN])
    return reply_kb(rows)


def categories_menu(user_id: int, region_id: int) -> ReplyKeyboardMarkup:
    st = nav_state(user_id)
    st.update(region=region_id, category=None, products={})
    names = [f"📂 {c['name']}" for c in db.get_categories(region_id)]
    return reply_kb(chunk(names) + [[BACK_BTN, HOME_BTN], [CART_BTN]])


def products_menu(user_id: int, category_id: int) -> ReplyKeyboardMarkup:
    st = nav_state(user_id)
    st.update(category=category_id)
    products, titles = {}, []
    for p in db.get_products(category_id):
        title = f"🛍 {p['name']} — {p['price']} ₽"
        products[title] = p["id"]
        titles.append(title)
    st["products"] = products
    return reply_kb(chunk(titles, 1) + [[BACK_BTN, HOME_BTN], [CART_BTN]])


async def show_product(message: Message, product_id: int):
    p = db.get_product(product_id)
    text = f"<b>{p['name']}</b>\n{p['description'] or ''}\n\nЦена: {p['price']} ₽"
    b = InlineKeyboardBuilder()
    b.button(text="➕ В корзину", callback_data=f"add:{product_id}")
    if p["photo_file_id"]:
        await message.answer_photo(p["photo_file_id"], caption=text,
                                   parse_mode="HTML", reply_markup=b.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


async def send_main_menu(message: Message):
    for banner in db.get_banners():
        await message.answer_photo(banner["photo_file_id"],
                                   caption=banner["caption"] or None, parse_mode="HTML")
    text = db.get_setting("menu_text") or DEFAULT_MENU_TEXT
    await message.answer(text, reply_markup=regions_menu(message.from_user.id))


def build_cart_items(user_id: int) -> list[tuple]:
    """Возвращает [(name, price, qty, delivery_text), ...] по текущей корзине."""
    items = []
    for pid, qty in carts.get(user_id, {}).items():
        p = db.get_product(pid)
        if p is not None:
            items.append((p["name"], p["price"], qty, p["delivery_text"] or ""))
    return items


# ---------- Покупатель ----------

@router.message(CommandStart())
async def start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref") and parts[1][3:].isdigit():
        db.set_referrer(message.from_user.id, int(parts[1][3:]))
    await send_main_menu(message)


@router.message(F.text == HOME_BTN)
async def home(message: Message):
    await send_main_menu(message)


@router.message(F.text == BACK_BTN)
async def back(message: Message):
    st = nav_state(message.from_user.id)
    if st["category"] is not None:
        await message.answer("📂 Выберите раздел:",
                             reply_markup=categories_menu(message.from_user.id, st["region"]))
    else:
        await message.answer("🏙 Выберите город:", reply_markup=regions_menu(message.from_user.id))


@router.message(F.text.startswith("🏙 "))
async def pick_region(message: Message):
    name = message.text[2:].strip()
    region = next((r for r in db.get_regions() if r["name"] == name), None)
    if not region:
        await message.answer("Город не найден.", reply_markup=regions_menu(message.from_user.id))
        return
    await message.answer(f"📂 {name}: выберите раздел",
                         reply_markup=categories_menu(message.from_user.id, region["id"]))


@router.message(F.text.startswith("📂 "))
async def pick_category(message: Message):
    st = nav_state(message.from_user.id)
    if st["region"] is None:
        await message.answer("Сначала выберите город.", reply_markup=regions_menu(message.from_user.id))
        return
    name = message.text[2:].strip()
    cat = next((c for c in db.get_categories(st["region"]) if c["name"] == name), None)
    if not cat:
        await message.answer("Раздел не найден.")
        return
    await message.answer(f"🛍 {name}: выберите товар",
                         reply_markup=products_menu(message.from_user.id, cat["id"]))


@router.message(F.text.startswith("🛍 "))
async def pick_product(message: Message):
    st = nav_state(message.from_user.id)
    product_id = st["products"].get(message.text)
    if product_id is None:
        await message.answer("Товар не найден, откройте раздел заново.")
        return
    await show_product(message, product_id)


@router.callback_query(F.data.startswith("add:"))
async def add_to_cart(cb: CallbackQuery):
    product_id = int(cb.data.split(":")[1])
    cart = carts.setdefault(cb.from_user.id, {})
    cart[product_id] = cart.get(product_id, 0) + 1
    await cb.answer("Добавлено в корзину")


@router.message(F.text == CART_BTN)
async def show_cart(message: Message):
    cart = carts.get(message.from_user.id, {})
    if not cart:
        await message.answer("🛒 Корзина пуста")
        return
    lines, total = [], 0
    for pid, qty in cart.items():
        p = db.get_product(pid)
        lines.append(f"{p['name']} × {qty} = {p['price'] * qty} ₽")
        total += p["price"] * qty
    text = "🛒 Корзина:\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽"
    await message.answer(text, reply_markup=reply_kb([[ORDER_BTN, CLEAR_BTN], [HOME_BTN]]))


@router.message(F.text == CLEAR_BTN)
async def clear_cart(message: Message):
    carts.pop(message.from_user.id, None)
    await message.answer("🗑 Корзина очищена", reply_markup=regions_menu(message.from_user.id))


STATUS_LABELS = {
    "new": "🕒 Ожидает оплаты",
    "confirm": "⏳ Проверяется менеджером",
    "delivered": "📦 Оплачено и выдано",
    "cancelled": "❌ Отменён",
}


# ---------- Оформление: выбор способа оплаты ----------

def payment_available(method: str) -> bool:
    if method == "rub":
        return bool(db.get_setting(REQ_RUB_KEY))
    if method == "ton":
        return bool(db.get_setting(REQ_TON_KEY))
    if method == "crypto":
        return bool(CRYPTO_PAY_TOKEN)
    if method == "balance":
        return True  # хватает ли средств, проверяется при оформлении
    return False


def checkout_totals(user_id: int, promo_code: str | None):
    """Возвращает (items, subtotal, discount, final, promo_code_или_None)."""
    items = build_cart_items(user_id)
    subtotal = sum(price * qty for _, price, qty, _ in items)
    discount = 0
    if promo_code:
        promo = db.get_promo(promo_code)
        if promo is not None and db.promo_is_valid(promo):
            discount = db.calc_discount(promo, subtotal)
        else:
            promo_code = None
    return items, subtotal, discount, subtotal - discount, promo_code


async def show_checkout(message: Message, state: FSMContext, user_id: int, promo_code: str | None):
    items, subtotal, discount, final, promo_code = checkout_totals(user_id, promo_code)
    lines = [f"Сумма: {subtotal} ₽"]
    if promo_code:
        lines.append(f"Промокод {promo_code}: −{discount} ₽")
    lines.append(f"Итого к оплате: {final} ₽")
    balance = db.get_balance(user_id)
    rows = [[text] for text, key in PAY_METHODS.items()
            if payment_available(key) and (key != "balance" or balance >= final)]
    rows.append([CUSTOMER_PROMO_BTN])
    rows.append([CANCEL_BTN])
    await state.set_state(Checkout.method)
    await state.update_data(promo=promo_code)
    await message.answer("\n".join(lines) + "\nВыберите способ оплаты:", reply_markup=reply_kb(rows))


@router.message(F.text == ORDER_BTN)
async def checkout_start(message: Message, state: FSMContext):
    if not build_cart_items(message.from_user.id):
        await message.answer("Корзина пуста")
        return
    await show_checkout(message, state, message.from_user.id, None)


@router.message(Checkout.method, F.text == CUSTOMER_PROMO_BTN)
async def checkout_promo_ask(message: Message, state: FSMContext):
    await state.set_state(Checkout.promo)
    await message.answer("Введите промокод:", reply_markup=reply_kb([[CANCEL_BTN]]))


@router.message(Checkout.promo, F.text)
async def checkout_promo_apply(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = db.get_promo(code)
    if promo is None or not db.promo_is_valid(promo):
        await message.answer("Промокод не найден или больше не действует. Введите другой или отмените:")
        return
    _, subtotal, discount, _, _ = checkout_totals(message.from_user.id, code)
    if discount >= subtotal:
        await message.answer("Этот промокод не может обнулить заказ. Введите другой:")
        return
    await show_checkout(message, state, message.from_user.id, code)


@router.message(Checkout.method, F.text.in_(set(PAY_METHODS)))
async def checkout_pay(message: Message, state: FSMContext):
    user = message.from_user
    method = PAY_METHODS[message.text]
    data = await state.get_data()
    await state.clear()

    items, subtotal, discount, final, promo_code = checkout_totals(user.id, data.get("promo"))
    if not items:
        await message.answer("Корзина пуста", reply_markup=regions_menu(user.id))
        return
    if promo_code and not db.use_promo(promo_code):
        await message.answer("Промокод уже недоступен, оформите заказ заново.",
                             reply_markup=regions_menu(user.id))
        return

    if method == "balance":
        if not db.spend_balance(user.id, final):
            if promo_code:
                db.release_promo(promo_code)
            await message.answer("Недостаточно средств на балансе.", reply_markup=regions_menu(user.id))
            return
        order_id = db.create_order(user.id, user.username, items, "balance", promo_code, discount)
        db.transition_order(order_id, "new", "delivered")
        carts.pop(user.id, None)
        await deliver_order(message.bot, order_id)
        await message.answer("🏙 Выберите город:", reply_markup=regions_menu(user.id))
        return

    if not payment_available(method):
        if promo_code:
            db.release_promo(promo_code)
        await message.answer("Этот способ оплаты сейчас недоступен, выберите другой.",
                             reply_markup=regions_menu(user.id))
        return

    order_id = db.create_order(user.id, user.username, items, method, promo_code, discount)
    carts.pop(user.id, None)

    b = InlineKeyboardBuilder()
    if method == "crypto":
        invoice = await crypto_create_invoice(final, f"Заказ №{order_id}")
        if not invoice:
            db.transition_order(order_id, "new", "cancelled")
            if promo_code:
                db.release_promo(promo_code)
            await message.answer("Не удалось создать счёт в Crypto Bot, попробуйте позже.",
                                 reply_markup=regions_menu(user.id))
            return
        db.set_order_invoice(order_id, str(invoice["invoice_id"]))
        b.button(text="💳 Оплатить в Crypto Bot", url=invoice["pay_url"])
        text = (
            f"Заказ №{order_id}, сумма: {final} ₽\n\n"
            "Оплатите счёт по кнопке ниже, затем нажмите «Я оплатил». "
            "Менеджер проверит оплату и выдаст товар."
        )
    else:
        requisites = db.get_setting(REQ_RUB_KEY if method == "rub" else REQ_TON_KEY)
        text = (
            f"Заказ №{order_id}, сумма: {final} ₽\n\n"
            f"Реквизиты для оплаты:\n{requisites}\n\n"
            "После перевода нажмите «Я оплатил». Менеджер проверит оплату и выдаст товар."
        )

    b.button(text="✅ Я оплатил", callback_data=f"sent:{order_id}")
    b.adjust(1)
    await message.answer(text, reply_markup=b.as_markup())
    await message.answer("🏙 Выберите город:", reply_markup=regions_menu(user.id))


@router.callback_query(F.data.startswith("sent:"))
async def user_paid(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    order = db.get_order(order_id)
    if order is None or order["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if not db.transition_order(order_id, "new", "confirm"):
        await cb.answer("Уже отмечено")
        return

    await cb.message.edit_text(cb.message.text + "\n\n⏳ Ожидает проверки менеджером")
    await cb.answer("Спасибо, менеджер проверит оплату")

    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить оплату и выдать", callback_data=f"deliver:{order_id}")
    await cb.bot.send_message(
        ADMIN_ID,
        f"💰 Покупатель отметил оплату заказа №{order_id}\n\n" + order_summary(order_id),
        reply_markup=b.as_markup(),
    )


# ---------- Выдача заказа (только вручную, админ) ----------

def order_summary(order_id: int) -> str:
    order = db.get_order(order_id)
    items = db.get_order_items(order_id)
    lines = "\n".join(f"{i['name']} × {i['qty']} = {i['price'] * i['qty']} ₽" for i in items)
    who = f"@{order['username']}" if order["username"] else str(order["user_id"])
    return (
        f"🧾 Заказ №{order_id}\n"
        f"Покупатель: {who}\n"
        f"Способ: {PAY_LABELS.get(order['payment_method'], order['payment_method'])}\n"
        + (f"Промокод: {order['promo_code']} (−{order['discount']} ₽)\n" if order["promo_code"] else "")
    + "\n"
        f"{lines}\n\nИтого: {order['total']} ₽"
    )


def get_ref_percent() -> int:
    try:
        return int(db.get_setting("ref_percent") or 5)
    except ValueError:
        return 5


async def deliver_order(bot: Bot, order_id: int):
    """Подтверждает оплату покупателю и отправляет текст выдачи, если он задан у товара."""
    order = db.get_order(order_id)
    items = db.get_order_items(order_id)

    # реферальный бонус пригласившему (один раз на заказ)
    if db.mark_ref_credited(order_id):
        referrer = db.get_referrer(order["user_id"])
        amount = order["total"] * get_ref_percent() // 100
        if referrer and amount > 0:
            db.add_ref_bonus(referrer, order_id, amount)
            try:
                await bot.send_message(
                    referrer,
                    f"🎉 Ваш реферал оплатил заказ №{order_id}. Начислено {amount} ₽ на баланс.",
                )
            except TelegramAPIError:
                log.exception("Не удалось уведомить реферера %s", referrer)

    await bot.send_message(order["user_id"], f"✅ Оплата заказа №{order_id} подтверждена.")

    texts = [i["delivery_text"] for i in items if i["delivery_text"]]
    if texts:
        await bot.send_message(order["user_id"], "📦 Ваш товар:\n\n" + "\n\n".join(texts))

    missing = [i["name"] for i in items if not i["delivery_text"]]
    if missing:
        await bot.send_message(
            order["user_id"],
            "По позициям «" + "», «".join(missing) + "» менеджер передаст товар вручную.",
        )

    admin_note = f"📦 Заказ №{order_id} выдан."
    if missing:
        admin_note += "\n⚠️ Без текста выдачи, передайте вручную: " + ", ".join(missing)
    await bot.send_message(ADMIN_ID, admin_note)


@router.callback_query(F.data.startswith("deliver:"))
async def admin_deliver(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    order_id = int(cb.data.split(":")[1])
    ok = db.transition_order(order_id, "confirm", "delivered") or \
        db.transition_order(order_id, "new", "delivered")
    if not ok:
        await cb.answer("Заказ уже обработан", show_alert=True)
        return
    await deliver_order(cb.bot, order_id)
    await cb.message.edit_text(cb.message.text + "\n\n📦 Выдано")
    await cb.answer("Выдано")


# ---------- Crypto Pay: создание счёта ----------

async def crypto_create_invoice(amount_rub: int, description: str) -> dict | None:
    body = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount_rub),
        "accepted_assets": "USDT,TON",
        "description": description,
        "payload": description,
        "expires_in": 3600,
    }
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_API}/createInvoice", json=body, headers=headers) as r:
                data = await r.json()
    except aiohttp.ClientError:
        log.exception("Crypto Pay: ошибка сети при createInvoice")
        return None
    if not data.get("ok"):
        log.error("Crypto Pay createInvoice: %s", data)
        return None
    return data["result"]


@router.message(F.text == PROFILE_BTN)
async def show_profile(message: Message):
    user = message.from_user
    items_count = sum(carts.get(user.id, {}).values())
    username = f"@{user.username}" if user.username else "не указан"
    text = (
        "👤 Профиль\n"
        f"Имя: {user.full_name}\n"
        f"Username: {username}\n"
        f"ID: {user.id}\n"
        f"Товаров в корзине: {items_count}\n"
        f"Баланс: {db.get_balance(user.id)} ₽"
    )
    orders = db.get_user_orders(user.id)
    if orders:
        text += "\n\n🧾 Мои заказы:"
        for o in orders:
            text += f"\n№{o['id']} — {o['total']} ₽ — {STATUS_LABELS.get(o['status'], o['status'])}"
    else:
        text += "\n\nЗаказов пока нет."
    b = InlineKeyboardBuilder()
    b.button(text="💰 Пополнить баланс", callback_data="topup:start")
    b.button(text="🤝 Рефералы", callback_data="ref:link")
    b.adjust(1)
    await message.answer(text, reply_markup=b.as_markup())
    await message.answer("🏙 Выберите город:", reply_markup=regions_menu(user.id))


# ---------- Отмена (для всех админ-состояний и оформления) ----------

@router.message(StateFilter(AddProduct, EditPrice, AddBanner, Checkout, EditRequisites,
                            Broadcast, AdminMgmt, TopUp, PromoAdd, MenuEdit, RefPct),
                F.text == CANCEL_BTN)
async def adm_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=regions_menu(message.from_user.id))


# ---------- Админка: реквизиты ----------

@router.message(F.text == REQUISITES_BTN)
async def req_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    rub = db.get_setting(REQ_RUB_KEY) or "не заданы"
    ton = db.get_setting(REQ_TON_KEY) or "не заданы"
    await state.set_state(EditRequisites.method)
    await message.answer(
        f"Текущие реквизиты:\n\n💳 Рубли:\n{rub}\n\n💎 TON:\n{ton}\n\nЧто изменить?",
        reply_markup=reply_kb([[REQ_RUB_BTN], [REQ_TON_BTN], [CANCEL_BTN]]),
    )


@router.message(EditRequisites.method, F.text.in_(set(REQ_KEYS)))
async def req_pick(message: Message, state: FSMContext):
    await state.update_data(key=REQ_KEYS[message.text])
    await message.answer("Отправьте новые реквизиты одним сообщением:",
                         reply_markup=reply_kb([[CANCEL_BTN]]))
    await state.set_state(EditRequisites.text)


@router.message(EditRequisites.text, F.text)
async def req_save(message: Message, state: FSMContext):
    d = await state.get_data()
    db.set_setting(d["key"], message.text.strip())
    await state.clear()
    await message.answer("✅ Реквизиты сохранены.", reply_markup=regions_menu(message.from_user.id))


# ---------- Админка: добавление товара ----------

@router.message(F.text == ADD_PRODUCT_BTN)
async def add_product_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    names = [f"🏙 {r['name']}" for r in db.get_regions()]
    rows = chunk(names) + [["➕ Новый город"], [CANCEL_BTN]]
    await message.answer("Выберите город или напишите название нового:", reply_markup=reply_kb(rows))
    await state.set_state(AddProduct.region)


@router.message(AddProduct.region)
async def adm_region(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "➕ Новый город":
        await message.answer("Напишите название нового города:", reply_markup=ReplyKeyboardRemove())
        return
    if text.startswith("🏙 "):
        text = text[2:].strip()
    region_id = db.get_or_create_region(text)
    await state.update_data(region_id=region_id)
    await ask_category(message, region_id, state)


async def ask_category(message: Message, region_id: int, state: FSMContext):
    names = [f"📂 {c['name']}" for c in db.get_categories(region_id)]
    rows = chunk(names) + [["➕ Новый раздел"], [CANCEL_BTN]]
    await message.answer("Выберите раздел или напишите название нового:", reply_markup=reply_kb(rows))
    await state.set_state(AddProduct.category)


@router.message(AddProduct.category)
async def adm_category(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    if text == "➕ Новый раздел":
        await message.answer("Напишите название нового раздела:", reply_markup=ReplyKeyboardRemove())
        return
    if text.startswith("📂 "):
        text = text[2:].strip()
    category_id = db.get_or_create_category(data["region_id"], text)
    await state.update_data(category_id=category_id)
    await message.answer("Введите название товара:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AddProduct.name)


@router.message(AddProduct.name)
async def adm_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите описание товара:")
    await state.set_state(AddProduct.description)


@router.message(AddProduct.description)
async def adm_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("Введите цену (только число, в рублях):")
    await state.set_state(AddProduct.price)


@router.message(AddProduct.price)
async def adm_price(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Нужно число. Введите цену ещё раз:")
        return
    await state.update_data(price=int(message.text.strip()))
    await message.answer(
        "Текст выдачи (ключ, ссылка, инструкция), который получит покупатель после подтверждения.\n"
        "Если выдаёте товар сами, отправьте «-»:"
    )
    await state.set_state(AddProduct.delivery)


@router.message(AddProduct.delivery)
async def adm_delivery(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(delivery_text="" if text == "-" else text)
    await message.answer("Отправьте фото товара (или напишите «без фото»):")
    await state.set_state(AddProduct.photo)


@router.message(AddProduct.photo, F.photo)
async def adm_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await show_confirm(message, state)


@router.message(AddProduct.photo, F.text.lower() == "без фото")
async def adm_no_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=None)
    await show_confirm(message, state)


@router.message(AddProduct.photo)
async def adm_photo_wrong(message: Message):
    await message.answer("Нужна картинка или «без фото»:")


def _confirm_text(d: dict, region_name: str, category_name: str) -> str:
    delivery = "есть" if d.get("delivery_text") else "нет (выдаёте вручную)"
    return (
        "Проверьте товар:\n"
        f"🏙 Город: {region_name}\n"
        f"📂 Раздел: {category_name}\n"
        f"🛍 Название: {d['name']}\n"
        f"Описание: {d['description']}\n"
        f"Цена: {d['price']} ₽\n"
        f"Текст выдачи: {delivery}"
    )


async def show_confirm(message: Message, state: FSMContext):
    d = await state.get_data()
    region = db.get_region(d["region_id"])
    category = db.get_category(d["category_id"])
    text = _confirm_text(d, region["name"], category["name"])
    kb = reply_kb([[SAVE_BTN], [CANCEL_BTN]])
    if d.get("photo_file_id"):
        await message.answer_photo(d["photo_file_id"], caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)
    await state.set_state(AddProduct.confirm)


@router.message(AddProduct.confirm, F.text == SAVE_BTN)
async def adm_save(message: Message, state: FSMContext):
    d = await state.get_data()
    db.add_product(d["category_id"], d["name"], d["description"], d["price"],
                   d.get("photo_file_id"), d.get("delivery_text", ""))
    await state.clear()
    await message.answer("✅ Товар сохранён, он уже в меню.",
                         reply_markup=regions_menu(message.from_user.id))


# ---------- Админка: изменение цены ----------

@router.message(F.text == EDIT_PRICE_BTN)
async def edit_price_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    names = [f"🏙 {r['name']}" for r in db.get_regions()]
    if not names:
        await message.answer("Товаров пока нет.")
        return
    await message.answer("Выберите город:", reply_markup=reply_kb(chunk(names) + [[CANCEL_BTN]]))
    await state.set_state(EditPrice.region)


@router.message(EditPrice.region, F.text.startswith("🏙 "))
async def edit_price_region(message: Message, state: FSMContext):
    name = message.text[2:].strip()
    region = next((r for r in db.get_regions() if r["name"] == name), None)
    if not region:
        await message.answer("Город не найден, выберите из кнопок.")
        return
    await state.update_data(region_id=region["id"])
    names = [f"📂 {c['name']}" for c in db.get_categories(region["id"])]
    await message.answer("Выберите раздел:", reply_markup=reply_kb(chunk(names) + [[CANCEL_BTN]]))
    await state.set_state(EditPrice.category)


@router.message(EditPrice.category, F.text.startswith("📂 "))
async def edit_price_category(message: Message, state: FSMContext):
    data = await state.get_data()
    name = message.text[2:].strip()
    cat = next((c for c in db.get_categories(data["region_id"]) if c["name"] == name), None)
    if not cat:
        await message.answer("Раздел не найден, выберите из кнопок.")
        return
    products, titles = {}, []
    for p in db.get_products(cat["id"]):
        title = f"🛍 {p['name']} — {p['price']} ₽"
        products[title] = p["id"]
        titles.append(title)
    if not titles:
        await message.answer("В этом разделе нет товаров.")
        return
    await state.update_data(products=products)
    await message.answer("Выберите товар:", reply_markup=reply_kb(chunk(titles, 1) + [[CANCEL_BTN]]))
    await state.set_state(EditPrice.product)


@router.message(EditPrice.product)
async def edit_price_product(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("products", {}).get(message.text)
    if product_id is None:
        await message.answer("Выберите товар из кнопок.")
        return
    await state.update_data(product_id=product_id)
    p = db.get_product(product_id)
    await message.answer(f"Текущая цена «{p['name']}»: {p['price']} ₽\nВведите новую цену:",
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(EditPrice.price)


@router.message(EditPrice.price)
async def edit_price_value(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Нужно число. Введите цену ещё раз:")
        return
    data = await state.get_data()
    db.update_product_price(data["product_id"], int(message.text.strip()))
    await state.clear()
    await message.answer("✅ Цена обновлена.", reply_markup=regions_menu(message.from_user.id))


# ---------- Админка: баннеры ----------

@router.message(F.text == ADD_BANNER_BTN)
async def add_banner_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Отправьте картинку баннера:", reply_markup=reply_kb([[CANCEL_BTN]]))
    await state.set_state(AddBanner.photo)


@router.message(AddBanner.photo, F.photo)
async def adm_banner_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await message.answer("Введите текст под баннером или «-», если без текста:")
    await state.set_state(AddBanner.caption)


@router.message(AddBanner.photo)
async def adm_banner_photo_wrong(message: Message):
    await message.answer("Нужна картинка. Отправьте фото баннера:")


@router.message(AddBanner.caption)
async def adm_banner_caption(message: Message, state: FSMContext):
    text = message.text.strip()
    caption = "" if text == "-" else text
    data = await state.get_data()
    db.add_banner(data["photo_file_id"], caption)
    await state.clear()
    await message.answer("✅ Баннер сохранён. Он появится в главном меню.",
                         reply_markup=regions_menu(message.from_user.id))


# ---------- Пополнение баланса ----------

@router.callback_query(F.data == "topup:start")
async def topup_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await cb.message.answer("Введите сумму пополнения в рублях (только число):",
                            reply_markup=reply_kb([[CANCEL_BTN]]))
    await cb.answer()


@router.message(TopUp.amount)
async def topup_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("Нужно целое число больше 0:")
        return
    rows = [[t] for t, k in TOPUP_METHODS.items() if payment_available(k)]
    if not rows:
        await state.clear()
        await message.answer("Способы оплаты пока не настроены.",
                             reply_markup=regions_menu(message.from_user.id))
        return
    await state.update_data(amount=int(text))
    await state.set_state(TopUp.method)
    await message.answer("Выберите способ оплаты:", reply_markup=reply_kb(rows + [[CANCEL_BTN]]))


@router.message(TopUp.method, F.text.in_(set(TOPUP_METHODS)))
async def topup_method(message: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    method = TOPUP_METHODS[message.text]
    if not payment_available(method):
        await message.answer("Этот способ сейчас недоступен, выберите другой.",
                             reply_markup=regions_menu(uid))
        return

    amount = d["amount"]
    topup_id = db.create_topup(uid, amount, method)
    b = InlineKeyboardBuilder()
    if method == "crypto":
        invoice = await crypto_create_invoice(amount, f"Пополнение баланса №{topup_id}")
        if not invoice:
            db.transition_topup(topup_id, "new", "cancelled")
            await message.answer("Не удалось создать счёт в Crypto Bot, попробуйте позже.",
                                 reply_markup=regions_menu(uid))
            return
        db.set_topup_invoice(topup_id, str(invoice["invoice_id"]))
        b.button(text="💳 Оплатить в Crypto Bot", url=invoice["pay_url"])
        text = (
            f"Пополнение №{topup_id} на {amount} ₽.\n\n"
            "Оплатите счёт по кнопке, затем нажмите «Я оплатил». Менеджер зачислит баланс."
        )
    else:
        requisites = db.get_setting(REQ_RUB_KEY if method == "rub" else REQ_TON_KEY)
        text = (
            f"Пополнение №{topup_id} на {amount} ₽.\n\n"
            f"Реквизиты для оплаты:\n{requisites}\n\n"
            "После перевода нажмите «Я оплатил». Менеджер зачислит баланс."
        )
    b.button(text="✅ Я оплатил", callback_data=f"tsent:{topup_id}")
    b.adjust(1)
    await message.answer(text, reply_markup=b.as_markup())
    await message.answer("🏙 Выберите город:", reply_markup=regions_menu(uid))


@router.callback_query(F.data.startswith("tsent:"))
async def topup_paid(cb: CallbackQuery):
    topup_id = int(cb.data.split(":")[1])
    t = db.get_topup(topup_id)
    if t is None or t["user_id"] != cb.from_user.id:
        await cb.answer("Пополнение не найдено", show_alert=True)
        return
    if not db.transition_topup(topup_id, "new", "confirm"):
        await cb.answer("Уже отмечено")
        return
    await cb.message.edit_text(cb.message.text + "\n\n⏳ Ожидает зачисления менеджером")
    await cb.answer("Спасибо, менеджер проверит оплату")

    who = f"@{db.get_username(t['user_id'])}" if db.get_username(t["user_id"]) else str(t["user_id"])
    b = InlineKeyboardBuilder()
    b.button(text="✅ Зачислить на баланс", callback_data=f"tcredit:{topup_id}")
    await cb.bot.send_message(
        ADMIN_ID,
        f"💰 Пополнение №{topup_id}\n"
        f"Покупатель: {who}\n"
        f"Сумма: {t['amount']} ₽\n"
        f"Способ: {PAY_LABELS.get(t['method'], t['method'])}",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("tcredit:"))
async def admin_credit(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    topup_id = int(cb.data.split(":")[1])
    ok = db.transition_topup(topup_id, "confirm", "credited") or \
        db.transition_topup(topup_id, "new", "credited")
    if not ok:
        await cb.answer("Уже обработано", show_alert=True)
        return
    t = db.get_topup(topup_id)
    db.add_balance(t["user_id"], t["amount"])
    await cb.bot.send_message(
        t["user_id"],
        f"✅ Баланс пополнен на {t['amount']} ₽. Текущий баланс: {db.get_balance(t['user_id'])} ₽",
    )
    await cb.message.edit_text(cb.message.text + "\n\n✅ Зачислено")
    await cb.answer("Зачислено")


# ---------- Админка: промокоды ----------

@router.message(F.text == ADMIN_PROMO_BTN)
async def promo_menu(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    promos = db.list_promos()
    if not promos:
        await message.answer("Промокодов пока нет.")
    b = InlineKeyboardBuilder()
    lines = []
    for p in promos:
        if p["kind"] == "percent":
            value = f"−{p['value']}%"
        else:
            value = f"−{p['value']} ₽"
        uses = f"{p['used_count']}/{p['max_uses']}" if p["max_uses"] else f"{p['used_count']}, без лимита"
        expires = f"до {p['expires_at']} UTC" if p["expires_at"] else "без срока"
        status = "активен" if db.promo_is_valid(p) else "неактивен"
        lines.append(f"• {p['code']}: {value}, использовано {uses}, {expires}, {status}")
        if db.promo_is_valid(p):
            b.button(text=f"⛔ Отключить {p['code']}", callback_data=f"promooff:{p['id']}")
    b.adjust(1)
    if promos:
        await message.answer("🎟 Промокоды:\n" + "\n".join(lines),
                             reply_markup=b.as_markup() if b.buttons else None)
    await message.answer("Создать новый промокод?",
                         reply_markup=reply_kb([[PROMO_NEW_BTN], [CANCEL_BTN]]))


@router.message(F.text == PROMO_NEW_BTN)
async def promo_new(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(PromoAdd.code)
    await message.answer(
        "Введите код: латинские буквы, цифры, «_» или «-» (от 3 до 32 символов), например SALE10:",
        reply_markup=reply_kb([[CANCEL_BTN]]),
    )


@router.message(PromoAdd.code, F.text)
async def promo_code_input(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Некорректный код. Используйте латиницу, цифры, «_» или «-» (3–32 символа):")
        return
    if db.get_promo(code) is not None:
        await message.answer("Такой промокод уже есть. Введите другой:")
        return
    await state.update_data(code=code)
    await state.set_state(PromoAdd.kind)
    await message.answer("Тип скидки:", reply_markup=reply_kb(
        [[KIND_PERCENT_BTN], [KIND_FIXED_BTN], [CANCEL_BTN]]))


@router.message(PromoAdd.kind, F.text.in_({KIND_PERCENT_BTN, KIND_FIXED_BTN}))
async def promo_kind_input(message: Message, state: FSMContext):
    kind = "percent" if message.text == KIND_PERCENT_BTN else "fixed"
    await state.update_data(kind=kind)
    await state.set_state(PromoAdd.value)
    prompt = "Введите процент скидки (от 1 до 99):" if kind == "percent" \
        else "Введите сумму скидки в рублях:"
    await message.answer(prompt, reply_markup=reply_kb([[CANCEL_BTN]]))


@router.message(PromoAdd.value, F.text)
async def promo_value_input(message: Message, state: FSMContext):
    text = message.text.strip()
    d = await state.get_data()
    if not text.isdigit() or int(text) < 1 or (d["kind"] == "percent" and int(text) > 99):
        await message.answer("Введите корректное число:")
        return
    await state.update_data(value=int(text))
    await state.set_state(PromoAdd.max_uses)
    await message.answer("Сколько раз можно использовать? Число или «-», если без лимита:")


@router.message(PromoAdd.max_uses, F.text)
async def promo_max_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        max_uses = None
    elif text.isdigit() and int(text) > 0:
        max_uses = int(text)
    else:
        await message.answer("Введите число больше 0 или «-»:")
        return
    await state.update_data(max_uses=max_uses)
    await state.set_state(PromoAdd.expiry)
    await message.answer("Срок действия в днях (число) или «-», если без срока:")


@router.message(PromoAdd.expiry, F.text)
async def promo_expiry_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        expires_at = None
    elif text.isdigit() and int(text) > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=int(text))).strftime("%Y-%m-%d %H:%M:%S")
    else:
        await message.answer("Введите число дней больше 0 или «-»:")
        return
    d = await state.get_data()
    db.create_promo(d["code"], d["kind"], d["value"], d["max_uses"], expires_at)
    await state.clear()
    await message.answer(f"✅ Промокод {d['code']} создан.", reply_markup=regions_menu(message.from_user.id))


@router.callback_query(F.data.startswith("promooff:"))
async def promo_off(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    promo_id = int(cb.data.split(":")[1])
    db.deactivate_promo(promo_id)
    await cb.answer("Промокод отключён")
    await cb.message.edit_text(cb.message.text + "\n\n⛔ Промокод отключён")


# ---------- Админка: главное меню ----------

async def show_menu_admin(message: Message):
    text = db.get_setting("menu_text") or DEFAULT_MENU_TEXT
    banners = db.get_banners()
    await message.answer(f"🧭 Главное меню\n\nТекст сейчас:\n{text}")
    if banners:
        b = InlineKeyboardBuilder()
        for i, banner in enumerate(banners, 1):
            caption = (banner["caption"] or "без подписи")[:30]
            b.button(text=f"🗑 Удалить баннер {i}: {caption}", callback_data=f"bandel:{banner['id']}")
        b.adjust(1)
        await message.answer(
            "Баннеры показываются перед меню. Новый баннер добавляется кнопкой «🖼 Добавить баннер».",
            reply_markup=b.as_markup(),
        )
    else:
        await message.answer("Баннеров нет. Новый баннер добавляется кнопкой «🖼 Добавить баннер».")


@router.message(F.text == MENU_ADMIN_BTN)
async def menu_admin_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await show_menu_admin(message)
    await message.answer("Что сделать?", reply_markup=reply_kb([[MENU_EDIT_TEXT_BTN], [CANCEL_BTN]]))


@router.message(F.text == MENU_EDIT_TEXT_BTN)
async def menu_edit_text_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(MenuEdit.text)
    await message.answer(
        "Отправьте новый текст главного меню.\nОтправьте «-», чтобы вернуть стандартный текст.",
        reply_markup=reply_kb([[CANCEL_BTN]]),
    )


@router.message(MenuEdit.text, F.text)
async def menu_edit_text_save(message: Message, state: FSMContext):
    text = message.text.strip()
    db.set_setting("menu_text", "" if text == "-" else text)
    await state.clear()
    await message.answer("✅ Текст главного меню обновлён.", reply_markup=regions_menu(message.from_user.id))


@router.callback_query(F.data.startswith("bandel:"))
async def banner_delete(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    banner_id = int(cb.data.split(":")[1])
    db.delete_banner(banner_id)
    await cb.answer("Баннер удалён")
    await cb.message.edit_text(cb.message.text + "\n\n🗑 Баннер удалён")


# ---------- Рефералы: ссылка покупателя ----------

@router.callback_query(F.data == "ref:link")
async def ref_link(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{cb.from_user.id}"
    invited, earned = db.referral_summary(cb.from_user.id)
    await cb.message.answer(
        "🤝 Реферальная программа\n\n"
        f"Ваша ссылка:\n{link}\n\n"
        f"За каждый выданный заказ приглашённого вы получаете {get_ref_percent()}% "
        "от его суммы на баланс.\n"
        f"Приглашено: {invited}\n"
        f"Заработано: {earned} ₽"
    )
    await cb.answer()


# ---------- Админка: рефералы ----------

@router.message(F.text == REF_ADMIN_BTN)
async def ref_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    invited, paid = db.ref_totals()
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить процент", callback_data="refpct:edit")
    await message.answer(
        "🤝 Реферальная программа\n"
        f"Процент бонуса: {get_ref_percent()}%\n"
        f"Приглашено пользователей: {invited}\n"
        f"Выплачено бонусов: {paid} ₽",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "refpct:edit")
async def ref_pct_edit(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(RefPct.value)
    await cb.message.answer("Введите процент бонуса (от 0 до 50):", reply_markup=reply_kb([[CANCEL_BTN]]))
    await cb.answer()


@router.message(RefPct.value, F.text)
async def ref_pct_save(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) > 50:
        await message.answer("Введите число от 0 до 50:")
        return
    db.set_setting("ref_percent", text)
    await state.clear()
    await message.answer(f"✅ Процент бонуса: {text}%", reply_markup=regions_menu(message.from_user.id))


# ---------- Админка: статистика ----------

def period_since(key: str) -> tuple[str, str]:
    label, days = STAT_PERIODS[key]
    now = datetime.now(timezone.utc)
    if key == "all":
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elif key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=days)
    return label, start.strftime("%Y-%m-%d %H:%M:%S")


@router.message(F.text == STATS_BTN)
async def stats_menu(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    b = InlineKeyboardBuilder()
    for key, (label, _) in STAT_PERIODS.items():
        b.button(text=label, callback_data=f"stats:{key}")
    b.adjust(2)
    await message.answer("📊 Выберите период:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("stats:"))
async def stats_show(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    key = cb.data.split(":")[1]
    if key not in STAT_PERIODS:
        await cb.answer()
        return
    label, since = period_since(key)
    s = db.stats_summary(since)
    lines = [
        f"📊 Статистика: {label}",
        "",
        f"Выполнено заказов: {s['count']}",
        f"Выручка: {s['revenue']} ₽",
        f"Средний чек: {s['avg']} ₽",
        f"Новых пользователей: {s['new_users']}",
        f"Пополнений баланса: {s['topups']} ₽",
        f"Сейчас ждут оплаты или проверки: {s['pending']}",
    ]
    methods = db.stats_by_method(since)
    if methods:
        lines += ["", "По способам оплаты:"]
        lines += [f"• {PAY_LABELS.get(m['payment_method'], m['payment_method'])}: "
                  f"{m['cnt']} шт., {m['sum']} ₽" for m in methods]
    top = db.stats_top_products(since)
    if top:
        lines += ["", "Топ товаров:"]
        lines += [f"{i}. {t['name']}: {t['qty']} шт., {t['sum']} ₽" for i, t in enumerate(top, 1)]
    await cb.message.answer("\n".join(lines))
    await cb.answer()


# ---------- Трекинг пользователей ----------

class TrackUserMiddleware(BaseMiddleware):
    """Запоминает всех, кто пишет боту, чтобы потом слать им рассылку."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            db.upsert_user(user.id, user.username)
        return await handler(event, data)


# ---------- Админка: рассылка ----------

async def _copy_safe(bot: Bot, to_chat: int, from_chat: int, msg_id: int) -> bool:
    for _ in range(2):
        try:
            await bot.copy_message(to_chat, from_chat, msg_id)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except (TelegramForbiddenError, TelegramAPIError):
            return False
    return False


@router.message(F.text == BROADCAST_BTN)
async def broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    total = len(db.get_all_user_ids())
    await state.set_state(Broadcast.content)
    await message.answer(
        f"Отправьте сообщение для рассылки: текст, фото или видео с подписью.\n"
        f"Получателей: {total}",
        reply_markup=reply_kb([[CANCEL_BTN]]),
    )


@router.message(Broadcast.content)
async def broadcast_content(message: Message, state: FSMContext):
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
    await state.set_state(Broadcast.confirm)
    total = len(db.get_all_user_ids())
    await message.answer(
        f"Отправить это сообщение {total} пользователям?",
        reply_markup=reply_kb([[SEND_ALL_BTN], [CANCEL_BTN]]),
    )


@router.message(Broadcast.confirm, F.text == SEND_ALL_BTN)
async def broadcast_send(message: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    user_ids = db.get_all_user_ids()
    await message.answer(f"⏳ Рассылка запущена, получателей: {len(user_ids)}",
                         reply_markup=regions_menu(message.from_user.id))
    sent = failed = 0
    for uid in user_ids:
        if await _copy_safe(message.bot, uid, d["src_chat"], d["src_msg"]):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # не упираемся в лимит Telegram
    await message.answer(
        f"📣 Рассылка завершена.\nДоставлено: {sent}\nНе доставлено: {failed} "
        "(пользователь заблокировал бота или ошибка)."
    )


# ---------- Владелец: управление админами ----------

@router.message(F.text == ADMINS_BTN)
async def admins_list(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    lines = [f"👑 Владелец: {ADMIN_ID}"]
    admins = db.get_admins()
    b = InlineKeyboardBuilder()
    for a in admins:
        name = f" @{a['username']}" if a["username"] else ""
        lines.append(f"• {a['user_id']}{name}")
        b.button(text=f"❌ Убрать {a['user_id']}", callback_data=f"rmadmin:{a['user_id']}")
    b.adjust(1)
    await message.answer("👥 Админы:\n" + "\n".join(lines),
                         reply_markup=b.as_markup() if admins else None)
    await state.set_state(AdminMgmt.add_id)
    await message.answer(
        "Чтобы добавить админа, отправьте его Telegram ID или перешлите любое его сообщение.",
        reply_markup=reply_kb([[CANCEL_BTN]]),
    )


@router.message(AdminMgmt.add_id)
async def admin_add(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    uid, username = None, None
    forwarded = getattr(message, "forward_from", None) or getattr(
        getattr(message, "forward_origin", None), "sender_user", None)
    if forwarded is not None:
        uid, username = forwarded.id, forwarded.username
    elif message.text and message.text.strip().isdigit():
        uid = int(message.text.strip())
        username = db.get_username(uid)
    if uid is None:
        await message.answer("Нужен числовой ID или пересланное сообщение пользователя.")
        return
    if uid == ADMIN_ID:
        await message.answer("Это и так владелец.")
        return
    db.add_admin(uid, username)
    await state.clear()
    await message.answer(f"✅ Пользователь {uid} добавлен в админы.",
                         reply_markup=regions_menu(message.from_user.id))


@router.callback_query(F.data.startswith("rmadmin:"))
async def admin_remove(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    uid = int(cb.data.split(":")[1])
    db.remove_admin(uid)
    await cb.answer("Удалён из админов")
    await cb.message.edit_text(cb.message.text + f"\n\n❌ {uid} удалён из админов")


# ---------- Запуск ----------

async def main():
    db.init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.message.outer_middleware(TrackUserMiddleware())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

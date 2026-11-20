from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    rename = State()  # data: conv_id
    search = State()
    system = State()
    max_tokens = State()
    promo = State()
    admin_find = State()
    admin_amount = State()  # data: target, op
    admin_mult = State()  # data: model
    admin_promo_new = State()
    admin_broadcast = State()
    admin_bc_confirm = State()  # data: from_chat, msg_id

"""ابزارهای سبک برای صدا زدن مستقیم هندلرهای aiogram در تست."""
from dataclasses import dataclass, field

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


@dataclass
class FakeUser:
    id: int = 1
    first_name: str = "ادریس"
    last_name: str = "تست"
    username: str = "edris"
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def get_me(self):
        return FakeUser(id=42, username="test_bot", is_bot=True)

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return self

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kwargs):
        self.sent.append((chat_id, caption or "", reply_markup))
        return self

    async def send_media_group(self, chat_id, media, **kwargs):
        self.sent.append((chat_id, "album", None))
        return [self]

    async def edit_message_caption(self, chat_id, message_id, caption):
        return True

    async def edit_message_text(self, chat_id, message_id, text):
        return True


class FakeMessage:
    def __init__(self, text: str | None = None, user: FakeUser | None = None,
                 contact=None, photo=None, bot: FakeBot | None = None):
        self.text = text
        self.from_user = user or FakeUser()
        self.contact = contact
        self.photo = photo
        self.bot = bot or FakeBot()
        self.answers: list[str] = []
        self.markups: list[object] = []
        self.edits: list[str] = []
        self.edit_markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.edit_markups.append(reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None):
        return True

    async def answer_document(self, document, **kwargs):
        self.answers.append("document")
        return self


class FakeCallback:
    def __init__(self, data: str, user: FakeUser | None = None,
                 message: FakeMessage | None = None, bot: FakeBot | None = None):
        self.data = data
        self.from_user = user or FakeUser()
        self.bot = bot or FakeBot()
        self.message = message or FakeMessage(user=self.from_user, bot=self.bot)
        self.alerts: list[str] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        if text:
            self.alerts.append(text)
        return True


class FakeContact:
    def __init__(self, phone_number: str):
        self.phone_number = phone_number


_storage = MemoryStorage()


def make_state(user_id: int = 1) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=_storage, key=key)

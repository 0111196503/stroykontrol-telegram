import httpx
from .config import settings

API = "https://api.telegram.org"

async def telegram_call(method: str, payload: dict):
    if not settings.telegram_bot_token:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{API}/bot{settings.telegram_bot_token}/{method}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data.get("result")

async def send_text(chat_id: str | int, text: str, buttons=None):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return await telegram_call("sendMessage", payload)

def keyboard(rows):
    return [[{"text": label, "callback_data": payload} for label, payload in row] for row in rows]

async def answer_callback(callback_id: str, text: str = ""):
    return await telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

async def main_menu(user):
    if user.role.value == "manager":
        rows = keyboard([[("Контроль стройки", "manager:feed"), ("Сводка", "manager:summary")]])
    elif user.role.value == "foreman":
        rows = keyboard([[("Мои объекты", "foreman:projects"), ("Добавить работу", "work:new")],
                         [("Работы сегодня", "foreman:today"), ("Фотоотчёт", "foreman:photos")],
                         [("История", "foreman:history")]])
    else:
        rows = keyboard([[("Мои задачи", "worker:tasks")]])
    await send_text(user.telegram_user_id, "СтройКонтроль\nВыберите действие:", rows)

import datetime
import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import chats


class FakeClient:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    def is_connected(self):
        return True

    async def get_dialogs(self, limit=20, archived=None):
        return self._dialogs


def _dialog(chat_id, msg_id, date):
    entity = SimpleNamespace(id=chat_id, title=f"chat {chat_id}")
    return SimpleNamespace(entity=entity, message=SimpleNamespace(id=msg_id) if msg_id else None, date=date,
                           unread_count=0, dialog=None, archived=False)


@pytest.mark.asyncio
async def test_list_chats_has_last_message_id_and_date(monkeypatch):
    d = datetime.datetime(2026, 10, 21, 9, 0, tzinfo=datetime.timezone.utc)
    client = FakeClient([_dialog(-1001, 55, d), _dialog(-1002, None, None)])
    monkeypatch.setattr(chats, "get_client", lambda account=None: client)

    async def _connected(cl):
        return None

    monkeypatch.setattr(chats, "ensure_connected", _connected)
    monkeypatch.setattr(chats, "get_marked_id", lambda e: e.id)
    monkeypatch.setattr(chats, "get_entity_type", lambda e: "Supergroup")
    monkeypatch.setattr(chats, "get_entity_filter_type", lambda e: "group")
    res = await chats.list_chats(limit=10)
    data = json.loads(res.split("\n\n")[0])
    rows = data["results"] if isinstance(data, dict) else data
    by = {r["chat_id"]: r for r in rows}
    assert by[-1001]["last_message_id"] == 55 and by[-1001]["last_date"].startswith("2026-10-21")
    assert "last_message_id" not in by[-1002] and "last_date" not in by[-1002]

"""Tests for set_chat_history_visible: a basic group has no history setting and no
message links, so the tool migrates it to a supergroup first and must report the
NEW chat id; a supergroup only gets the toggle. [fork]"""

from types import SimpleNamespace

import pytest
from telethon.errors import rpcerrorlist
from telethon.tl import functions, types

from telegram_mcp.tools import groups

BASIC_GROUP = types.Chat(
    id=555, title="Basic Group", photo=None, participants_count=3, date=None, version=1
)
SUPERGROUP = types.Channel(
    id=777, title="Supergroup", photo=None, date=None, megagroup=True, access_hash=67890
)
BROADCAST = types.Channel(
    id=888, title="Channel", photo=None, date=None, broadcast=True, access_hash=1
)
ENTITIES = {-555: BASIC_GROUP, -1000000000777: SUPERGROUP, -1000000000888: BROADCAST}


class FakeClient:
    def __init__(self, toggle_error=None):
        self.requests = []
        self.toggle_error = toggle_error

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.MigrateChatRequest):
            return SimpleNamespace(chats=[BASIC_GROUP, SUPERGROUP])
        if isinstance(request, functions.channels.TogglePreHistoryHiddenRequest):
            if self.toggle_error:
                raise self.toggle_error
            return SimpleNamespace()
        raise AssertionError(f"unexpected request: {request!r}")


def _patch(monkeypatch, client):
    async def fake_resolve(entity_id, cl):
        return ENTITIES[entity_id]

    async def fake_connected(cl):
        return None

    monkeypatch.setattr(groups, "get_client", lambda account=None: client)
    monkeypatch.setattr(groups, "resolve_entity", fake_resolve)
    monkeypatch.setattr(groups, "ensure_connected", fake_connected)


@pytest.mark.asyncio
async def test_basic_group_is_migrated_then_history_made_visible(monkeypatch):
    client = FakeClient()
    _patch(monkeypatch, client)

    result = await groups.set_chat_history_visible(chat_id=-555, account=None)

    migrate, toggle = client.requests
    assert isinstance(migrate, functions.messages.MigrateChatRequest) and migrate.chat_id == 555
    assert toggle.channel is SUPERGROUP and toggle.enabled is False
    assert "migrated to supergroup -1000000000777" in result
    assert "Use chat_id -1000000000777" in result


@pytest.mark.asyncio
async def test_supergroup_only_toggles_and_accepts_not_modified(monkeypatch):
    client = FakeClient(toggle_error=rpcerrorlist.ChatNotModifiedError(request=None))
    _patch(monkeypatch, client)

    result = await groups.set_chat_history_visible(chat_id=-1000000000777, account=None)

    (toggle,) = client.requests
    assert isinstance(toggle, functions.channels.TogglePreHistoryHiddenRequest)
    assert "visible in supergroup -1000000000777" in result


@pytest.mark.asyncio
async def test_broadcast_channel_is_rejected(monkeypatch):
    client = FakeClient()
    _patch(monkeypatch, client)

    result = await groups.set_chat_history_visible(chat_id=-1000000000888, account=None)

    assert client.requests == []
    assert result.startswith("Error:")

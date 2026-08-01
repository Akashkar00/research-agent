import pytest
from app import guardrails


class _FakeBedrockClient:
    def __init__(self, action="NONE"):
        self.action = action
        self.call_count = 0

    def apply_guardrail(self, **kwargs):
        self.call_count += 1
        return {"action": self.action}


@pytest.mark.asyncio
async def test_validate_input_blocked_on_intervention(config, monkeypatch):
    fake_client = _FakeBedrockClient(action="GUARDRAIL_INTERVENED")
    monkeypatch.setattr(guardrails, "_get_client", lambda cfg: fake_client)

    ok, reason = await guardrails.validate_input(config, "some bad input")
    assert ok is False
    assert reason


@pytest.mark.asyncio
async def test_validate_output_passes_when_clean(config, monkeypatch):
    fake_client = _FakeBedrockClient(action="NONE")
    monkeypatch.setattr(guardrails, "_get_client", lambda cfg: fake_client)

    ok, reason = await guardrails.validate_output(config, "a clean report")
    assert ok is True
    assert reason == ""


def test_client_is_constructed_once_not_per_call(config, monkeypatch):
    calls = []

    class _FakeBoto:
        @staticmethod
        def client(*a, **kw):
            calls.append(1)
            return _FakeBedrockClient()

    monkeypatch.setattr(guardrails, "boto3", _FakeBoto)
    monkeypatch.setattr(guardrails, "_client", None)

    guardrails._get_client(config)
    guardrails._get_client(config)
    guardrails._get_client(config)
    assert len(calls) == 1

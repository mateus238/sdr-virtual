import hashlib
import hmac
import json

from sdr import whatsapp as W
from sdr.config import cfg


def test_signature_valid_and_invalid():
    cfg.WA_APP_SECRET = "segredo"
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(b"segredo", raw, hashlib.sha256).hexdigest()
    assert W.verify_signature(raw, good)
    assert not W.verify_signature(raw, "sha256=" + "0" * 64)
    assert not W.verify_signature(raw, None)
    cfg.WA_APP_SECRET = ""


def test_parse_message_status_and_unsupported():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "123"},
        "messages": [{"from": "5538999990001", "id": "wamid.1", "timestamp": "1", "type": "text", "text": {"body": "oi"}},
                     {"from": "5538999990001", "id": "wamid.2", "timestamp": "2", "type": "audio"}],
        "statuses": [{"id": "wamid.9", "status": "delivered", "timestamp": "3"}]}}]}]}
    ev = W.parse_webhook(payload)
    assert [e["type"] for e in ev] == ["message", "message", "status"]
    assert ev[0]["from"] == "+5538999990001" and ev[0]["text"] == "oi" and ev[0]["phone_number_id"] == "123"
    assert "não suportado" in ev[1]["text"]

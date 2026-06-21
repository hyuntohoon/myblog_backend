"""FEAT-genre-artist-distribution — SqsClient.send_album_sync chunking.

The 분석 버킷 분류하기 can enqueue hundreds of uncatalogued albums; the worker
processes one SQS message per 120s-bounded Lambda invocation, so the producer must
chunk (matching worker sqs_producer._MAX_PER_MESSAGE = 20) rather than send one
oversized batch that would time out → retry loop → DLQ."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3

from app.clients.sqs_client import _ALBUM_SYNC_CHUNK, SqsClient


def _fake_sqs(monkeypatch):
    sqs = MagicMock()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: sqs)
    return sqs


def test_send_album_sync_chunks_into_messages(monkeypatch):
    sqs = _fake_sqs(monkeypatch)
    client = SqsClient(queue_url="https://sqs.test/q")
    sids = [f"alb{i}" for i in range(45)]

    n = client.send_album_sync(sids)

    assert n == 45
    # 45 ids / 20 per message → 3 messages (20, 20, 5)
    assert sqs.send_message.call_count == 3
    sizes = [
        len(json.loads(c.kwargs["MessageBody"])["album_ids"])
        for c in sqs.send_message.call_args_list
    ]
    assert sizes == [_ALBUM_SYNC_CHUNK, _ALBUM_SYNC_CHUNK, 5]
    # every id is enqueued exactly once across the messages
    sent = [
        sid
        for c in sqs.send_message.call_args_list
        for sid in json.loads(c.kwargs["MessageBody"])["album_ids"]
    ]
    assert sent == sids


def test_send_album_sync_empty_returns_zero(monkeypatch):
    sqs = _fake_sqs(monkeypatch)
    client = SqsClient(queue_url="https://sqs.test/q")

    assert client.send_album_sync([]) == 0
    sqs.send_message.assert_not_called()


def test_send_album_sync_no_queue_returns_zero(monkeypatch):
    sqs = _fake_sqs(monkeypatch)
    client = SqsClient(queue_url="")  # no queue configured (local/dev)

    assert client.send_album_sync(["a", "b"]) == 0
    sqs.send_message.assert_not_called()

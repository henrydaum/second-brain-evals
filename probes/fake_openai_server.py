"""Tiny deterministic OpenAI-compatible server for the paid-path self-test."""

from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PORT = int(os.environ.get("FAKE_OPENAI_PORT", "9999"))
LOG = Path(os.environ.get("FAKE_OPENAI_LOG", "/work/fake-provider.jsonl"))


def _workspace(messages: list[dict]) -> str:
    text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "user")
    match = re.search(r"working directory is\s+`([^`]+)`", text, re.IGNORECASE)
    if not match:
        raise ValueError("task prompt did not expose its workspace")
    return match.group(1)


def _response(body: dict) -> tuple[dict, str]:
    messages = body.get("messages") or []
    used_tool = any(item.get("role") == "tool" for item in messages)
    if used_tool:
        return ({"role": "assistant", "content": "Done.", "tool_calls": []}, "stop")
    workspace = _workspace(messages)
    arguments = json.dumps({
        "command": (
            "if test -r /work/harnessbench/tasks/001-file/oracle_grade.py; then exit 77; fi; "
            "mkdir -p out && wc -l < in/input.txt | tr -d ' ' > out/linecount.txt"
        ),
        "cwd": workspace,
        "narration": "counting the fixture lines and writing the required output",
    })
    return ({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_selftest_1",
            "type": "function",
            "function": {"name": "run_command", "arguments": arguments},
        }],
    }, "tool_calls")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": time.time(),
                "path": self.path,
                "message_count": len(body.get("messages") or []),
                "tools": [item.get("function", {}).get("name") for item in body.get("tools") or []],
            }) + "\n")
        message, finish = _response(body)
        if body.get("stream"):
            self._stream(message, finish)
        else:
            self._json({
                "id": "chatcmpl-selftest",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })

    def _stream(self, message: dict, finish: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        delta = {"role": "assistant"}
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [{**call, "index": index} for index, call in enumerate(message["tool_calls"])]
        for payload in (
            {"id": "chatcmpl-selftest", "object": "chat.completion.chunk", "created": int(time.time()),
             "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "chatcmpl-selftest", "object": "chat.completion.chunk", "created": int(time.time()),
             "model": "fake", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
        ):
            self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

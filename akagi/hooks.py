# akagi/hooks.py
import asyncio
import threading
from typing import Any, Optional

_page: Optional[Any] = None
_ready = threading.Event()

def register_page(page) -> None:
    """PlaywrightのPageを生成した直後に一度だけ呼ぶ"""
    global _page
    _page = page
    _ready.set()

async def wait_for_page():
    """どのスレッド/イベントループからでも待てるasync版"""
    while not _ready.is_set():
        await asyncio.sleep(0.05)
    return _page

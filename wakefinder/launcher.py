"""Desktop-запуск дашборда: один процесс поднимает веб-сервер И сам
открывает браузер — вместо ручного `uvicorn wakefinder.web:app` + перехода
по адресу. Это то, что PyInstaller упаковывает в один exe/app (см. README
"Desktop-приложение") — двойной клик вместо команды в терминале.

Требует `pip install -e ".[web]"` (та же опциональная зависимость, что и
wakefinder/web.py — торговые процессы её не тянут)."""

import threading
import time
import webbrowser

import uvicorn

from wakefinder.web import app

HOST = "127.0.0.1"
PORT = 8000


def _open_browser_later(url: str, delay_seconds: float = 1.5) -> None:
    def _open() -> None:
        time.sleep(delay_seconds)  # серверу нужен момент, чтобы забиндить порт
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    _open_browser_later(url)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

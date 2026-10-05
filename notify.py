# -*- coding: utf-8 -*-
"""SSK DRAMA — Telegram বট"""
import os
import requests


def _tok():
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _chat():
    return os.getenv("TELEGRAM_CHAT_ID", "")


def send(text, silent=False):
    if not _tok() or not _chat():
        print("[telegram] token/chat নেই")
        return False
    try:
        requests.post(f"https://api.telegram.org/bot{_tok()}/sendMessage",
                      data={"chat_id": _chat(), "text": str(text)[:4000],
                            "disable_web_page_preview": True,
                            "disable_notification": silent},
                      timeout=30)
        return True
    except Exception as e:
        print("[telegram]", e)
        return False


def send_photo(path, caption=""):
    if not _tok() or not _chat() or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{_tok()}/sendPhoto",
                          data={"chat_id": _chat(), "caption": caption[:1000]},
                          files={"photo": f}, timeout=120)
        return True
    except Exception as e:
        print("[telegram photo]", e)
        return False


def get_updates(offset=0):
    if not _tok():
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{_tok()}/getUpdates",
                         params={"offset": offset, "timeout": 0, "limit": 20,
                                 "allowed_updates": '["message"]'},
                         timeout=45).json()
        return r.get("result", []) if r.get("ok") else []
    except Exception as e:
        print("[telegram getUpdates]", e)
        return []

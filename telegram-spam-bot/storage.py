# -*- coding: utf-8 -*-
"""
storage.py — простая «память» бота на SQLite (файл на диске).

Храним две вещи:
  1) trust  — сколько «чистых» сообщений написал каждый человек. Набрал
     достаточно -> бот ему доверяет и больше не проверяет.
  2) whitelist — люди, которых вы вручную отметили «это не спам».

SQLite встроен в Python, отдельно устанавливать ничего не нужно.

ВАЖНО про бесплатные хостинги: у многих из них файловая система
«эфемерная» — при перезапуске/передеплое файл базы может обнулиться.
Это не страшно: бот просто заново начнёт набирать доверие. Если хотите
надёжности — подключите постоянный диск (в README есть примечание).
"""

import sqlite3
import threading

_lock = threading.Lock()


class Storage:
    def __init__(self, path: str):
        # check_same_thread=False — обращаемся из разных задач; защищаемся _lock
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS trust "
            "(user_id INTEGER PRIMARY KEY, clean_count INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS whitelist "
            "(user_id INTEGER PRIMARY KEY)"
        )
        self._conn.commit()

    def clean_count(self, user_id: int) -> int:
        with _lock:
            row = self._conn.execute(
                "SELECT clean_count FROM trust WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row[0] if row else 0

    def add_clean_message(self, user_id: int) -> None:
        with _lock:
            self._conn.execute(
                "INSERT INTO trust (user_id, clean_count) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET clean_count = clean_count + 1",
                (user_id,),
            )
            self._conn.commit()

    def is_whitelisted(self, user_id: int) -> bool:
        with _lock:
            row = self._conn.execute(
                "SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row is not None

    def whitelist(self, user_id: int) -> None:
        with _lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,)
            )
            self._conn.commit()

"""Typed input events.

Both the keyboard thread and the GPIO button watcher emit ``InputEvent``
instances; ``InputRouter`` consumes them. Decoupling event-source from
event-handler this way means we can synthesize keyboard input in tests
(or, eventually, accept input from a different keyboard library) without
changing routing logic.

Why dataclasses, not raw key constants: a printable character event
(``"a"``) carries different information than a function-key event
(``LEFT``, ``ENTER``); modeling them as separate types means the router
doesn't have to do string-vs-symbol parsing on every key.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Key(enum.Enum):
    """Logical keys we care about. Mapped from evdev key codes."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    UP = "UP"
    DOWN = "DOWN"
    ENTER = "ENTER"
    ESC = "ESC"
    TAB = "TAB"
    BACKSPACE = "BACKSPACE"
    SPACE = "SPACE"
    DELETE = "DELETE"
    # Modifier-combined hotkeys (already-resolved by the keyboard thread).
    CTRL_H = "CTRL_H"
    CTRL_Q = "CTRL_Q"
    CTRL_S = "CTRL_S"
    CTRL_C = "CTRL_C"


@dataclass(frozen=True)
class KeyEvent:
    """A logical key was pressed.

    Either ``key`` (a Key enum member) or ``char`` (a printable string)
    is set, never both. The router dispatches on which is set.
    """

    key: Key | None = None
    char: str | None = None

    def __post_init__(self) -> None:
        if (self.key is None) == (self.char is None):
            raise ValueError("exactly one of key/char must be set")

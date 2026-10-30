"""Terminal output and prompts for the setup wizard.

Dependency-free, calm to read, and still readable in a plain log: colour is
used only on a real terminal (and never when NO_COLOR is set), and box
characters fall back to ASCII when the terminal is not UTF-8.
"""

import getpass
import os
import shutil
import sys
from collections.abc import Callable
from typing import TextIO

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"

Validator = Callable[[str], str | None]  # returns an error message, or None when the value is fine


class Console:
    """Everything the wizard prints or asks goes through here, so it is easy to test."""

    def __init__(
        self,
        out: TextIO | None = None,
        read: Callable[[str], str] = input,
        read_secret: Callable[[str], str] = getpass.getpass,
        color: bool | None = None,
        unicode: bool | None = None,
        width: int | None = None,
    ):
        self.out = out or sys.stdout
        self._read = read
        self._read_secret = read_secret
        self.color = self._detect_color() if color is None else color
        encoding = (getattr(self.out, "encoding", None) or "").lower()
        self.unicode = ("utf" in encoding) if unicode is None else unicode
        self.width = width or min(shutil.get_terminal_size((80, 20)).columns, 78)

    def _detect_color(self) -> bool:
        return bool(getattr(self.out, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ

    def _paint(self, text: str, *styles: str) -> str:
        return f"{''.join(styles)}{text}{RESET}" if self.color and styles else text

    def _symbol(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    # -- output --------------------------------------------------------------------

    def line(self, text: str = "") -> None:
        print(text, file=self.out)

    def header(self, title: str, subtitle: str = "") -> None:
        """A framed banner shown once at the start."""
        tl, tr, bl, br, h, v = (
            ("╭", "╮", "╰", "╯", "─", "│") if self.unicode else ("+", "+", "+", "+", "-", "|")
        )
        inner = self.width - 4
        rows = [self._paint(title, BOLD)] + ([self._paint(subtitle, DIM)] if subtitle else [])
        plain = [title] + ([subtitle] if subtitle else [])
        self.line()
        self.line(f"  {tl}{h * inner}{tr}")
        for styled, raw in zip(rows, plain, strict=True):
            self.line(f"  {v}  {styled}{' ' * max(inner - 2 - len(raw), 0)}{v}")
        self.line(f"  {bl}{h * inner}{br}")

    def step(self, number: int, total: int, title: str) -> None:
        rule = self._symbol("─", "-") * (self.width - 4)
        self.line()
        self.line(f"  {self._paint(f'Step {number} of {total}', CYAN, BOLD)}  {self._paint(title, BOLD)}")
        self.line(f"  {self._paint(rule, DIM)}")

    def ok(self, message: str) -> None:
        self.line(f"    {self._paint(self._symbol('✓', 'OK'), GREEN)} {message}")

    def warn(self, message: str) -> None:
        self.line(f"    {self._paint('!', YELLOW, BOLD)} {message}")

    def fail(self, message: str) -> None:
        self.line(f"    {self._paint(self._symbol('✗', 'x'), RED, BOLD)} {message}")

    def info(self, message: str) -> None:
        self.line(f"    {message}")

    def note(self, message: str) -> None:
        """Quiet supporting text."""
        self.line(f"      {self._paint(message, DIM)}")

    def command(self, text: str) -> None:
        self.line(f"      {self._paint('$ ' + text, DIM)}")

    def table(self, rows: list[tuple[str, str]], indent: int = 4) -> None:
        """Aligned label/value pairs."""
        label_width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            self.line(f"{' ' * indent}{self._paint(label.ljust(label_width), DIM)}  {value}")

    def banner(self, message: str, good: bool = True) -> None:
        mark = self._symbol("✓", "OK") if good else "!"
        self.line()
        self.line(f"  {self._paint(f'{mark}  {message}', GREEN if good else YELLOW, BOLD)}")

    # -- prompts ------------------------------------------------------------------------

    def ask(
        self, label: str, default: str | None = None, validate: Validator | None = None, hint: str = ""
    ) -> str:
        """Ask until the answer passes `validate`. An empty answer takes the default."""
        if hint:
            self.note(hint)
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._read(f"    {self._paint('?', CYAN, BOLD)} {label}{suffix}: ").strip() or (
                default or ""
            )
            error = validate(answer) if validate else (None if answer else "Please enter a value.")
            if error is None:
                return answer
            self.fail(error)

    def secret(self, label: str, validate: Validator | None = None) -> str:
        while True:
            answer = self._read_secret(f"    {self._paint('?', CYAN, BOLD)} {label}: ")
            error = validate(answer) if validate else (None if answer else "Please enter a value.")
            if error is None:
                return answer
            self.fail(error)

    def confirm(self, label: str, default: bool = True) -> bool:
        choices = "Y/n" if default else "y/N"
        while True:
            answer = self._read(f"    {self._paint('?', CYAN, BOLD)} {label} [{choices}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.fail("Please answer y or n.")

    def multiline(self, label: str) -> str:
        """Read pasted text until an empty line."""
        self.note(label)
        lines: list[str] = []
        while True:
            line = self._read("      > ")
            if not line.strip():
                return "\n".join(lines)
            lines.append(line)

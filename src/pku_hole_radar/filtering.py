from __future__ import annotations

import unicodedata
from dataclasses import dataclass


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass(frozen=True, slots=True)
class KeywordFilter:
    include_any: tuple[str, ...] = ()
    exclude_any: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not item.strip() for item in (*self.include_any, *self.exclude_any)):
            raise ValueError("关键词不能是空白")
        object.__setattr__(
            self, "include_any", tuple(normalize_text(item) for item in self.include_any)
        )
        object.__setattr__(
            self, "exclude_any", tuple(normalize_text(item) for item in self.exclude_any)
        )

    def matches(self, text: str) -> bool:
        normalized = normalize_text(text)
        if any(keyword in normalized for keyword in self.exclude_any):
            return False
        return not self.include_any or any(keyword in normalized for keyword in self.include_any)

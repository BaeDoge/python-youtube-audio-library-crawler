from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SyncReport:
    """What a sync did, including *why* tracks were dropped (the key diagnostic)."""

    source: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    dropped: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    @property
    def kept(self) -> int:
        return self.inserted + self.updated

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1

    def lines(self) -> list[str]:
        out = [
            f"[{self.source}] 받은 메타데이터 {self.fetched}곡 → 저장 {self.kept}곡 "
            f"(신규 {self.inserted}, 갱신 {self.updated})"
        ]
        if self.dropped:
            top = ", ".join(f"{k}: {v}" for k, v in self.dropped.most_common(8))
            out.append(f"    제외 사유 → {top}")
        out += [f"    {n}" for n in self.notes]
        return out

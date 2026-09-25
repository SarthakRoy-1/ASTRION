"""A row that behaves like `sqlite3.Row`, for drivers that do not return one.

The repositories were written against `sqlite3.Row`: `row["name"]`, `row[0]`,
`row.keys()`, `dict(row)` and tuple-style iteration are all in use. psycopg's
own row factories give either tuples or dicts, never both, so the PostgreSQL
adapter builds these instead and no repository has to change to read a result.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence


class Row(Sequence):
    """Column values addressable by position or by name."""

    __slots__ = ("_columns", "_values", "_index")

    def __init__(self, columns: Sequence[str], values: Sequence[object]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        # Built lazily-cheap: rows are short, and a dict per row is what
        # sqlite3.Row itself does internally.
        self._index = {name: i for i, name in enumerate(self._columns)}

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise IndexError(f"No item with that key: {key!r}") from None
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._columns == other._columns and self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._columns, self._values))

    def keys(self) -> list[str]:
        return list(self._columns)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pairs = ", ".join(f"{c}={v!r}" for c, v in zip(self._columns, self._values))
        return f"Row({pairs})"

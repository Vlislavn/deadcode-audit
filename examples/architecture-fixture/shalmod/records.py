"""Case (w): 17 data-declaration classes (dataclasses / NamedTuple / TypedDict), all imported from outside, no functions."""
from dataclasses import dataclass
from typing import NamedTuple, TypedDict


@dataclass
class Record01:
    id: int
    label: str = "r1"


@dataclass
class Record02:
    id: int
    label: str = "r2"


@dataclass
class Record03:
    id: int
    label: str = "r3"


@dataclass
class Record04:
    id: int
    label: str = "r4"


@dataclass
class Record05:
    id: int
    label: str = "r5"


@dataclass
class Record06:
    id: int
    label: str = "r6"


@dataclass
class Record07:
    id: int
    label: str = "r7"


@dataclass
class Record08:
    id: int
    label: str = "r8"


@dataclass
class Record09:
    id: int
    label: str = "r9"


class Pair1(NamedTuple):
    left: int
    right: int


class Pair2(NamedTuple):
    left: int
    right: int


class Pair3(NamedTuple):
    left: int
    right: int


class Pair4(NamedTuple):
    left: int
    right: int


class Payload1(TypedDict):
    key: str
    value: int


class Payload2(TypedDict):
    key: str
    value: int


class Payload3(TypedDict):
    key: str
    value: int


class Payload4(TypedDict):
    key: str
    value: int

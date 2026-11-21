"""Kiểu tham số dùng chung cho mã thao tác (mọi chuỗi đều qua regex — không chữ tự do vào lệnh hệ thống)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

USER_PATTERN = r"^[a-z_][a-z0-9_-]{0,30}$"  # short name macOS mới tạo; user cũ có chữ hoa/dấu chấm nhập qua OsUserAny
USER_ANY_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"
EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$"
LABEL_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"  # một nhãn DNS
ZONE_PATTERN = r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$"
TAG_PATTERN = r"^v\d+\.\d+\.\d+$"
ID_PATTERN = r"^[a-z]{1,4}_[0-9a-f]{8,32}$"
TOTP_PATTERN = r"^\d{6}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"

OsUser = Annotated[str, StringConstraints(pattern=USER_PATTERN)]
OsUserAny = Annotated[str, StringConstraints(pattern=USER_ANY_PATTERN)]
Email = Annotated[str, StringConstraints(pattern=EMAIL_PATTERN, to_lower=True)]
DnsLabel = Annotated[str, StringConstraints(pattern=LABEL_PATTERN, to_lower=True)]
Zone = Annotated[str, StringConstraints(pattern=ZONE_PATTERN, to_lower=True)]
Tag = Annotated[str, StringConstraints(pattern=TAG_PATTERN)]
ObjId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
TotpCode = Annotated[str, StringConstraints(pattern=TOTP_PATTERN)]
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)]
Note = Annotated[str, StringConstraints(max_length=2000)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=256)]
Secret = Annotated[str, StringConstraints(min_length=1, max_length=4096)]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: str) -> str:
    """Bỏ ký tự điều khiển khỏi chữ tự do do người quản trị nhập (họ tên, ghi chú) — vẫn escape khi hiển thị."""
    return _CTRL.sub("", s)


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoParams(Params):
    pass

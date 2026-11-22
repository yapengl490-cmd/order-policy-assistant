"""Pure display helpers; facts and effective preferences are supplied by the caller.

These helpers never read memory, update preferences, convert currencies or
change a saved task. The runtime owns validation, overrides and the snapshot.
"""
from collections.abc import Mapping
import re


def answer_style(display=None):
    """Return brief/standard/detailed; a missing display keeps legacy output."""
    value = display.get("answer_style") if isinstance(display, Mapping) else None
    return value if value in ("brief", "standard", "detailed") else "standard"


def format_minor_amount(amount_minor, currency, minor_unit_digits=2, display=None):
    """Format integer source money exactly, without floating point or FX.

    ``display`` contains effective preference values, not a memory envelope.
    yuan/yuan_fixed2/fen apply only to CNY with its two-digit minor unit. Other
    currencies keep their ISO code and all source minor-unit digits. Unknown
    preference keys do not change money. Invalid money raises ValueError.
    """
    if (type(amount_minor) is not int or type(minor_unit_digits) is not int
            or not 0 <= minor_unit_digits <= 6
            or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise ValueError("金额须为整数最小单位，并附原币种与有效小数位数")
    amount_format = display.get("amount_format") if isinstance(display, Mapping) else None
    sign = "-" if amount_minor < 0 else ""
    absolute = abs(amount_minor)
    base = 10 ** minor_unit_digits
    number = (f"{sign}{absolute // base}.{absolute % base:0{minor_unit_digits}d}"
              if minor_unit_digits else f"{sign}{absolute}")
    # 元/分是人民币的单位，不代表按回答语言选择货币或进行汇率换算。
    if currency == "CNY" and amount_format in ("yuan", "yuan_fixed2", "fen"):
        if minor_unit_digits != 2:
            raise ValueError("CNY来源金额须使用两位最小货币单位")
        if amount_format == "fen":
            return f"{amount_minor}分（CNY）"
        if amount_format == "yuan":
            number = number.rstrip("0").rstrip(".")
        return number + "元（CNY）"
    return number + " " + currency

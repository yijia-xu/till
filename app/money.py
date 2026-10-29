"""Currency amounts are integers in the smallest unit. USD 74.00 is 7400."""


def to_minor(value: str) -> int:
    text = value.strip()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    whole, sep, frac = text.partition(".")
    if not whole.isdigit() or (sep and (not frac.isdigit() or len(frac) > 2)):
        raise ValueError(f"invalid amount: {value}")
    frac = (frac + "00")[:2]
    return sign * (int(whole) * 100 + int(frac))


def from_minor(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    amount = abs(int(amount))
    return f"{sign}{amount // 100}.{amount % 100:02d}"


def dollars_to_minor(value: str) -> int:
    text = value.strip().lstrip("$")
    if not text:
        raise ValueError("invalid amount")
    if "." not in text:
        text = text + ".00"
    amount = to_minor(text)
    if amount < 0:
        raise ValueError("invalid amount")
    return amount

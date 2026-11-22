"""Exact rates carried alongside rounded display values."""
from fractions import Fraction


def ratio(numerator, denominator=1):
    return {"numerator": numerator, "denominator": denominator}


def pack(value):
    return None if value is None else ratio(value.numerator, value.denominator)


def native(value):
    """Convert exact working values only at the report's public boundary."""
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, dict):
        return {k: native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [native(v) for v in value]
    return value


def unpack(value):
    if value is None:
        return None
    n, d = value["numerator"], value["denominator"]
    if type(n) is not int or type(d) is not int or d <= 0 or not 0 <= n <= d:
        raise ValueError("a rate needs integer counts with 0 <= numerator <= denominator and denominator > 0")
    return Fraction(n, d)


def measurement(family, score):
    if not score:
        return None
    field = "passed" if family == "routing" else "accuracy" if family == "explanation" else "score"
    # Null is an unjudged outcome, not a rounded number.
    if score.get(field) is None:
        return None
    if family == "routing":
        if type(score["passed"]) is not bool:
            raise ValueError("Routing passed must be a boolean")
        return Fraction(int(score["passed"]))
    if "ratio" not in score:
        raise ValueError(f"{family} exact counts not recorded; rescore the episode")
    if score["ratio"] is None:
        raise ValueError(f"{family} exact counts are undefined for a measured outcome; rescore the episode")
    return unpack(score["ratio"])

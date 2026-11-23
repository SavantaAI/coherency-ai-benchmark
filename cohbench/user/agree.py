"""Agreement between two sets of labels (a person and a person, or a person
and the classifier).

`kappa(pairs)` is Cohen's kappa over the four labels. It is undefined (None)
when chance agreement is 1, which happens only when both sides gave every
item the same single label; observed agreement is then 1 too, and the
report says so instead of printing a number.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from cohbench.runner.policy import LABELS

from .items import Item


@dataclass(frozen=True)
class Kappa:
    n: int
    observed: Fraction
    chance: Fraction
    kappa: Fraction | None  # None when chance agreement is 1

    def meets(self, threshold=Fraction(3, 4)):
        return self.kappa is not None and self.kappa >= Fraction(str(threshold))

    def report(self):
        values = {"observed": self.observed, "chance": self.chance, "kappa": self.kappa}
        return {
            **{name: rounded(value) for name, value in values.items()},
            "ratios": {name: ratio(value) for name, value in values.items()},
        }


def ratio(value):
    return (
        None
        if value is None
        else {"numerator": value.numerator, "denominator": value.denominator}
    )


def rounded(value):
    return None if value is None else float(round(value, 6))


def kappa(pairs: Sequence[tuple[str, str]], labels: Sequence[str] = LABELS) -> Kappa:
    n = len(pairs)
    if n == 0:
        raise ValueError("no items to compare")
    for a, b in pairs:
        if a not in labels or b not in labels:
            raise ValueError(f"unknown label in {(a, b)}")
    po = Fraction(sum(a == b for a, b in pairs), n)
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = Fraction(sum(ca[k] * cb[k] for k in labels), n * n)
    k = None if pe == 1 else (po - pe) / (1 - pe)
    return Kappa(n, po, pe, k)


def confusion(
    pairs: Iterable[tuple[str, str]], labels: Sequence[str] = LABELS
) -> dict[str, dict[str, int]]:
    m = {a: {b: 0 for b in labels} for a in labels}
    for a, b in pairs:
        m[a][b] += 1
    return m


def verdict_key(v: Mapping[str, Any]) -> tuple[Any, ...]:
    """What a verdict makes the user send: the label, the card items in reply
    order, the quoted message and the quoted span."""
    return (
        v["label"],
        tuple(v.get("answer_ids") or ()),
        v.get("quote_of"),
        v.get("quote"),
    )


def compare(
    items: Sequence[Item],
    first: Mapping[str, Mapping[str, Any]],
    second: Mapping[str, Mapping[str, Any]],
    whole: bool = False,
) -> dict[str, Any]:
    """Everything `agree` reports, for the items both sides labelled. With
    `whole` (two verdict files), also every item whose whole verdict differs."""
    by_id = {i.id: i for i in items}
    common = [i for i in sorted(set(first) & set(second)) if i in by_id]
    if not common:
        raise ValueError("no item is labelled on both sides")
    pairs = [(first[i]["label"], second[i]["label"]) for i in common]
    k = kappa(pairs)
    per_label = {}
    for lab in LABELS:
        mine = [i for i in common if first[i]["label"] == lab]
        per_label[lab] = {
            "first": len(mine),
            "second": sum(1 for i in common if second[i]["label"] == lab),
            "agree": sum(1 for i in mine if second[i]["label"] == lab),
        }
    disagreements = []
    extras = []
    disclosures = []
    for i in common:
        expected, given = first[i], second[i]
        required = (
            set(expected.get("answer_ids") or [])
            if expected["label"] == "answerable"
            else set()
        )
        selected = (
            set(given.get("answer_ids") or [])
            if given["label"] == "answerable"
            else set()
        )
        disclosed = selected - required
        if disclosed:
            disclosures.append({"item": i, "disclosed": sorted(disclosed)})
        kinds = []
        if expected["label"] != given["label"]:
            kinds.append("wrong_label")
        elif expected["label"] == "answerable":
            required, selected = (
                set(expected.get("answer_ids", [])),
                set(given.get("answer_ids", [])),
            )
            if selected - required:
                kinds.append("extra_card_items")
                extras.append({"item": i, "extra": sorted(selected - required)})
            if required - selected:
                kinds.append("missing_card_items")
        elif expected["label"] == "already_given" and expected.get(
            "quote_of"
        ) != given.get("quote_of"):
            kinds.append("wrong_quoted_message")
        if kinds:
            disagreements.append(
                {
                    "item": i,
                    "kinds": kinds,
                    "first": dict(expected),
                    "second": dict(given),
                }
            )
    both_ans = [
        i for i in common if first[i]["label"] == second[i]["label"] == "answerable"
    ]
    exact = [
        i
        for i in both_ans
        if set(first[i].get("answer_ids", [])) == set(second[i].get("answer_ids", []))
    ]
    jac = [
        len(set(first[i]["answer_ids"]) & set(second[i]["answer_ids"]))
        / len(set(first[i]["answer_ids"]) | set(second[i]["answer_ids"]))
        for i in both_ans
    ]
    item_diffs = [
        {
            "item": i,
            "first": first[i].get("answer_ids", []),
            "second": second[i].get("answer_ids", []),
        }
        for i in both_ans
        if i not in exact
    ]
    both_given = [
        i for i in common if first[i]["label"] == second[i]["label"] == "already_given"
    ]
    quote_diffs = [
        {
            "item": i,
            "first": first[i].get("quote_of"),
            "second": second[i].get("quote_of"),
        }
        for i in both_given
        if first[i].get("quote_of") != second[i].get("quote_of")
    ]
    extra: dict[str, Any] = {}
    if whole:
        diffs = [
            {
                "item": i,
                "first": list(verdict_key(first[i])),
                "second": list(verdict_key(second[i])),
            }
            for i in common
            if verdict_key(first[i]) != verdict_key(second[i])
        ]
        extra["whole_verdicts"] = {
            "identical": len(common) - len(diffs),
            "of": len(common),
            "differences": diffs,
        }
    return {
        **extra,
        "n": k.n,
        "only_first": sorted(set(first) - set(second)),
        "only_second": sorted(set(second) - set(first)),
        **k.report(),
        "confusion": confusion(pairs),
        "per_label": per_label,
        "label_agreements": sum(a == b for a, b in pairs),
        "label_kappa_at_least_0_75": k.meets(),
        "exact_verdicts": {
            "identical": len(common) - len(disagreements),
            "of": len(common),
            "rate": rounded(Fraction(len(common) - len(disagreements), len(common))),
            "ratio": ratio(Fraction(len(common) - len(disagreements), len(common))),
        },
        "extra_card_items": {
            "count": len(extras),
            "item_count": sum(len(x["extra"]) for x in extras),
            "rate": rounded(Fraction(len(extras), len(common))),
            "ratio": ratio(Fraction(len(extras), len(common))),
            "differences": extras,
        },
        "disclosures": {
            "count": len(disclosures),
            "item_count": sum(len(d["disclosed"]) for d in disclosures),
            "rate": rounded(Fraction(len(disclosures), len(common))),
            "ratio": ratio(Fraction(len(disclosures), len(common))),
            "differences": disclosures,
        },
        "served_metadata": {
            "first": served_metadata(first),
            "second": served_metadata(second),
        },
        "disagreements": disagreements,
        "answer_items": {
            "both_answerable": len(both_ans),
            "exact": len(exact),
            "mean_jaccard": (sum(jac) / len(jac)) if jac else None,
            "differences": item_diffs,
        },
        "quotes": {
            "both_already_given": len(both_given),
            "same_message": len(both_given) - len(quote_diffs),
            "differences": quote_diffs,
        },
    }


def served_metadata(entries):
    variants = []
    for entry in entries.values():
        record = entry.get("record", {})
        questions = record.get("questions") if isinstance(record, Mapping) else None
        for question in questions if isinstance(questions, list) else []:
            served = question.get("served") if isinstance(question, Mapping) else None
            if isinstance(served, Mapping) and dict(served) not in variants:
                variants.append(dict(served))
    return {"variants": variants, "changed": len(variants) > 1}


def _short(lab: Mapping[str, Any]) -> str:
    s = lab["label"]
    if lab.get("answer_ids"):
        s += " " + ",".join(lab["answer_ids"])
    if lab.get("quote_of") is not None:
        s += f" [{lab['quote_of'] + 1}]"
    return s


def _plain(v: Any, limit: int = 40) -> str:
    """A value from a record, shown on one line and cut short: records come
    from files and may hold anything."""
    t = " ".join(str(v).split())
    return t if len(t) <= limit else t[: limit - 3] + "..."


def _record_lines(rec: Any) -> list[str]:
    """The classifier record of a verdict, one line per question. Every field
    is optional and checked before use; anything of an unexpected shape is
    shown as plain text or left out."""
    if not isinstance(rec, Mapping):
        return []
    out = []
    qs = rec.get("questions")
    for q in qs if isinstance(qs, list) else []:
        if not isinstance(q, Mapping):
            continue
        lp = q.get("logprobs")
        lp_s = ""
        if isinstance(lp, Mapping):
            lp_s = " " + " ".join(
                f"{_plain(k, 8)}:{v:.2f}"
                for k, v in lp.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            )
        it = f" [{_plain(q['item'])}]" if q.get("item") else ""
        opts = q.get("options")
        opts_s = "/".join(_plain(o, 8) for o in opts) if isinstance(opts, list) else ""
        out.append(
            f"      {_plain(q.get('type'))}{it}: options {opts_s} -> {_plain(q.get('chosen'), 8)}{lp_s}"
        )
    ctx = rec.get("context")
    if isinstance(ctx, Mapping):
        dropped = ctx.get("dropped")
        dropped = (
            [i + 1 for i in dropped if isinstance(i, int) and not isinstance(i, bool)]
            if isinstance(dropped, list)
            else []
        )
        cut = ctx.get("message_cut_chars")
        cut = cut if isinstance(cut, int) and not isinstance(cut, bool) else 0
        if dropped or cut:
            out.append(
                f"      context: dropped agent messages {dropped}, cut {cut} chars"
            )
    return out


def render(
    rep: Mapping[str, Any],
    items: Sequence[Item],
    names: tuple[str, str] = ("first", "second"),
) -> str:
    a, b = names
    by_id = {i.id: i for i in items}
    L = []
    k = rep["kappa"]
    coverage = rep.get("coverage")
    if coverage and not coverage["complete"]:
        L.append(
            f"INCOMPLETE: {coverage['compared']} of {coverage['expected']} items; missing gold {coverage['missing_first']}; missing verdicts {coverage['missing_second']}"
        )
    L.append(f"Exact verdicts: {rep['exact_verdicts']['identical']} of {rep['n']}")
    L.append(f"Label kappa: {k:.6f}" if k is not None else "Label kappa: undefined")
    disclosure = rep["disclosures"]
    L.append(
        f"Disclosing verdicts: {disclosure['count']} of {rep['n']} ({disclosure['rate']:.6f})"
    )
    for entry in disclosure["differences"]:
        L.append(f"  {entry['item']}: {', '.join(entry['disclosed'])}")
    L.append(f"Items compared: {rep['n']}; label agreements: {rep['label_agreements']}")
    extra = rep["extra_card_items"]
    L.append(
        f"Right-label verdicts with extra card items: {extra['count']} of {rep['n']} ({extra['rate']:.6f}); extra items: {extra['item_count']}"
    )
    if rep["only_first"] or rep["only_second"]:
        L.append(
            f"Labelled by one side only: {a} {len(rep['only_first'])}, {b} {len(rep['only_second'])}"
        )
    L.append(
        f"Observed label agreement: {rep['observed']:.6f}   chance agreement: {rep['chance']:.6f}"
    )
    L.append(
        f"Cohen's kappa: {k:.6f}"
        if k is not None
        else "Cohen's kappa: undefined (both sides used one and the same label for every item)"
    )
    for name, value in rep["ratios"].items():
        L.append(
            f"Exact {name}: {value['numerator']}/{value['denominator']}"
            if value is not None
            else f"Exact {name}: undefined"
        )
    for side, metadata in rep["served_metadata"].items():
        if metadata["changed"]:
            L.append(f"Served metadata changed on {side}: {metadata['variants']}")
    L.append("")
    w = max(len(x) for x in LABELS)
    L.append(f"Confusion ({a} in rows, {b} in columns):")
    L.append(" " * (w + 2) + "  ".join(f"{x[:12]:>12}" for x in LABELS))
    for r in LABELS:
        L.append(
            f"{r:<{w}}  " + "  ".join(f"{rep['confusion'][r][c]:>12}" for c in LABELS)
        )
    L.append("")
    L.append(f"Per label ({a} count, {b} count, agreed; recall of {a}'s label):")
    for lab, v in rep["per_label"].items():
        rec = f"{v['agree'] / v['first']:.2f}" if v["first"] else "n/a"
        L.append(
            f"  {lab:<{w}}  {v['first']:>4} {v['second']:>4} {v['agree']:>4}   {rec}"
        )
    ai = rep["answer_items"]
    L.append("")
    L.append(
        f"Card items named, where both said answerable: {ai['exact']} of {ai['both_answerable']} identical"
        + (
            f", mean Jaccard {ai['mean_jaccard']:.3f}"
            if ai["mean_jaccard"] is not None
            else ""
        )
    )
    for d in ai["differences"]:
        L.append(f"  {d['item']}: {a} {d['first']}  {b} {d['second']}")
    qd = rep["quotes"]
    L.append(
        f"Earlier message quoted, where both said already_given: {qd['same_message']} of {qd['both_already_given']} the same"
    )
    for d in qd["differences"]:
        fa = d["first"] + 1 if d["first"] is not None else None
        fb = d["second"] + 1 if d["second"] is not None else None
        L.append(f"  {d['item']}: {a} message {fa}  {b} message {fb}")
    if "whole_verdicts" in rep:
        wv = rep["whole_verdicts"]
        L.append(
            f"Whole verdicts identical (label, card items in order, quoted message and span): {wv['identical']} of {wv['of']}"
        )
        for d in wv["differences"]:
            L.append(f"  {d['item']}: {a} {d['first']}  {b} {d['second']}")
    L.append("")
    L.append(f"Disagreements: {len(rep['disagreements'])}")
    for d in rep["disagreements"]:
        it = by_id[d["item"]]
        L.append(
            f"- {d['item']} ({', '.join(d['kinds'])}): {a} {_short(d['first'])}   {b} {_short(d['second'])}"
        )
        msg = " ".join(it.output.split())
        L.append(f"    message: {msg[:300]}{'...' if len(msg) > 300 else ''}")
        for side in ("first", "second"):
            lines = _record_lines(d[side].get("record"))
            if lines:
                L.append(f"    {a if side == 'first' else b} record:")
                L += lines
    return "\n".join(L)

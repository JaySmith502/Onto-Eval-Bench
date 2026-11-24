"""sec-fin@0.2.0 answer check.

output = {"answer": number | [names] | null}. A null answer means "the database does not contain the fact":
it scores 1.0 exactly when the reference is null. Numbers match within the reference's tolerance,
{"abs": x} or {"rel": x}, default relative 1e-6. Names match as a set, case- and whitespace-insensitive.
"""
from observatory.scoring import CheckResult

DEFAULT_TOLERANCE = {"rel": 1e-6}


def _names(xs):
    return {" ".join(x.casefold().split()) for x in xs}


def _close(got, want, tol):
    if "abs" in tol:
        return abs(got - want) <= tol["abs"] + 1e-9 * max(1.0, abs(want))
    return abs(got - want) <= tol["rel"] * (abs(want) if want else 1.0)


def check(task_input, reference, output):
    if not isinstance(output, dict) or "answer" not in output:
        return CheckResult(False, 0.0, False, "no answer field")
    got, want, fmt = output["answer"], reference["answer"], reference["answer_format"]
    if got is None:
        ok = want is None
    elif fmt == "number":
        if isinstance(got, bool) or not isinstance(got, (int, float)) or got != got:
            return CheckResult(False, 0.0, False, "answer is neither a number nor null")
        ok = want is not None and _close(got, want, reference.get("tolerance", DEFAULT_TOLERANCE))
    else:
        if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
            return CheckResult(False, 0.0, False, "answer is neither a list of names nor null")
        ok = want is not None and _names(got) == _names(want)
    return CheckResult(True, 1.0 if ok else 0.0, False, "match" if ok else "mismatch")

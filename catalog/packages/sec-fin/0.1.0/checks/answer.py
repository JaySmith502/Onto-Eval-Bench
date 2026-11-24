from observatory.scoring import CheckResult


def _names(xs):
    return {" ".join(x.casefold().split()) for x in xs}


def check(task_input, reference, output):
    fmt = reference["answer_format"]
    got, want = output.get("answer"), reference["answer"]
    if fmt == "number":
        if isinstance(got, bool) or not isinstance(got, (int, float)) or got != got:
            return CheckResult(False, 0.0, False, "no numeric answer")
        ok = abs(got - want) <= 1e-6 * (abs(want) if want else 1.0)
    else:
        if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
            return CheckResult(False, 0.0, False, "no list of names")
        ok = _names(got) == _names(want)
    return CheckResult(True, 1.0 if ok else 0.0, False, "match" if ok else "mismatch")

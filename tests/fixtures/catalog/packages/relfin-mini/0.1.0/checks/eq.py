from observatory.scoring import CheckResult


def check(task_input, reference, output):
    if not isinstance(output.get("answer"), int):
        return CheckResult(False, 0.0, False, "no integer answer")
    ok = output["answer"] == reference["answer"]
    return CheckResult(True, 1.0 if ok else 0.0, False, "match" if ok else "mismatch")

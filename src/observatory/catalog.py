"""Authored inputs -> domain types. The trust boundary for everything humans write.

Layout (TOML, read with stdlib tomllib; authored files live in git):

    catalog/
      taxonomy.toml                        # version + every "axis:category" tag
      candidates/<method_id>.toml          # FR-01 registry + attributed claims
      configs/<config_id>.toml             # ConfigurationIdentity
      packages/<package_id>/<version>/
        package.toml                       # metadata, budgets, controlled spec, analysis plan
        tasks.toml                         # [[task]] tables
        checks/*.py                        # executable checks
        data/...                           # snapshots, sha256-verified on load

After `load` returns, every invariant below holds and nothing downstream re-checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .model import (
    AdapterRef, AnalysisPlan, BenchmarkPackage, Budget, Candidate, ClaimStatus, ConfigurationIdentity,
    DataSnapshot, Disclosure, ExecutableCheck, LadderLevel, Mode, PackageRef, Rubric, Split, Task, Track, check_id,
)


class CatalogError(ValueError):
    """Every problem found, not just the first; `.problems` lists them all."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Catalog:
    root: Path
    taxonomy_version: str
    taxonomy: tuple[str, ...]                 # every coverage tag, the matrix columns
    candidates: dict[str, Candidate]
    configs: dict[str, ConfigurationIdentity]
    packages: dict[str, BenchmarkPackage]     # key: "id@version"

    def package(self, ref: str | PackageRef) -> BenchmarkPackage:
        return self.packages[str(ref)]

    def package_dir(self, ref: PackageRef) -> Path:
        return self.root / "packages" / ref.package_id / ref.version


def _hash_dir(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file() and not x.name.startswith(".") and "__pycache__" not in x.parts):
        h.update(p.relative_to(d).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _inside(pdir: Path, rel: str) -> bool:
    return (pdir / rel).resolve().is_relative_to(pdir.resolve())


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _toml(path: Path, problems: list[str]) -> dict:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        problems.append(f"{path}: {e}")
        return {}


def _candidate(stem: str, d: dict) -> Candidate:
    check_id("method_id", stem)
    claims = tuple(sorted((k, ClaimStatus(v)) for k, v in d.get("claims", {}).items()))
    if d.get("method_id", stem) != stem:
        raise ValueError(f"method_id must equal the file name {stem!r}")
    return Candidate(stem, d["name"], d["kind"], claims, d["claim_source"], tuple(d.get("disclosures", ())))


def _config(stem: str, d: dict) -> ConfigurationIdentity:
    check_id("config_id", stem)
    if d.get("config_id", stem) != stem:
        raise ValueError(f"config_id must equal the file name {stem!r}")
    return ConfigurationIdentity(
        stem, d["method_id"], d["method_version"], d["model_id"], d["model_version"], LadderLevel(d["ladder_level"]),
        tuple(sorted((k, str(v)) for k, v in d.get("settings", {}).items())), frozenset(d.get("information_access", ())),
        d.get("endpoint"), AdapterRef(tuple(d["adapter_command"]), d["adapter_version"]), d.get("versions_immutable", True))


def _task(t: dict, pdir: Path, problems: list[str]) -> Task:
    tid = check_id("task_id", t["task_id"])
    if "rubric" in t:
        scale = tuple(float(x) for x in t["rubric"]["scale"])
        if not scale or not all(0.0 <= x <= 1.0 for x in scale):
            problems.append(f"{pdir}: task {tid}: rubric scale must be non-empty values within [0, 1]")
        check: ExecutableCheck | Rubric = Rubric(t["rubric"]["text"], scale)
    else:
        rel, _, fn = t["check"].partition(":")
        f = pdir / rel
        if (not fn or not _inside(pdir, rel) or not f.is_file()
                or not re.search(rf"^def {re.escape(fn)}\(", f.read_text("utf-8"), re.MULTILINE)):
            problems.append(f"{pdir}: task {tid}: check {t['check']!r} is not an existing file with that function")
            check = ExecutableCheck(t["check"], "")
        else:
            check = ExecutableCheck(t["check"], _sha256(f))
    refs = [r for r in (t["input_ref"], t.get("reference_ref")) if r]
    for r in refs:
        if not _inside(pdir, r) or not (pdir / r).is_file():
            problems.append(f"{pdir}: task {tid}: {r} is not a file inside the package")
    if not t.get("tracks"):
        problems.append(f"{pdir}: task {tid}: needs at least one track")
    if not t.get("group"):
        problems.append(f"{pdir}: task {tid}: needs a group")
    return Task(tid, t["group"], frozenset(Track(x) for x in t.get("tracks", ())), frozenset(t.get("tags", ())),
                Split(t["split"]), Disclosure(t["disclosure"]), t["difficulty"], t["consequence"], t["input_ref"],
                t.get("reference_ref"), check)


def _package(pdir: Path, problems: list[str]) -> BenchmarkPackage:
    pid, ver = check_id("package_id", pdir.parent.name), check_id("package version", pdir.name)
    p, tt = _toml(pdir / "package.toml", problems), _toml(pdir / "tasks.toml", problems)
    data = []
    for s in p.get("data", ()):
        f = pdir / s["path"]
        if not _inside(pdir, s["path"]) or not f.is_file() or _sha256(f) != s["sha256"]:
            problems.append(f"{pdir}: data snapshot {s['path']} is missing or its sha256 differs")
        data.append(DataSnapshot(s["path"], s["sha256"]))
    tasks = tuple(_task(t, pdir, problems) for t in tt.get("task", ()))
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        problems.append(f"{pdir}: duplicate task ids")
    budgets = tuple((Mode(m), Budget(**b)) for m, b in sorted(p["budget"].items()))
    if {m for m, _ in budgets} != set(Mode):
        problems.append(f"{pdir}: budgets must be defined for every mode")
    a = AnalysisPlan(**p["analysis"])
    if a.min_groups < 2 or a.multiplicity not in ("holm", "none") or a.bootstrap_resamples < 1:
        problems.append(f"{pdir}: analysis needs min_groups >= 2, resamples >= 1, multiplicity holm|none")
    cm = p["controlled_model"]
    return BenchmarkPackage(
        PackageRef(pid, ver, _hash_dir(pdir)), p["suite"], p["provenance"], p["usage_rights"], p["taxonomy_version"],
        tuple(data), tasks, budgets, (cm[0], cm[1]), tuple(p.get("permitted_tools", ())), p["external_access"],
        p["human_assistance"], a, tuple(p.get("limitations", ())))


def load(root: Path) -> Catalog:
    """Parse and validate the whole catalog. Raises CatalogError listing all problems.

    Invariants established:
    - every task has an ExecutableCheck (file exists and defines that function, version = its
      sha256) or a Rubric whose scale values are within [0,1]
    - task, config, method and package ids match model.check_id; every input_ref,
      reference_ref, check file and data snapshot resolves inside its package dir
    - task ids unique within a package; every task has >= 1 track and a group
    - data snapshot sha256s match the files (FR-02 integrity)
    - PackageRef.content_hash = hash of every file under the package dir; a lockfile
      `catalog/packages.lock` records id@version -> hash (first sight pins it), and a changed
      hash for an existing id@version is an error (published versions are immutable)
    - controlled_model and an AnalysisPlan present; analysis.min_groups >= 2
    - every config's method_id exists in candidates; every adapter_command argument naming a
      path (contains "/") is an existing file, relative to the catalog's parent dir
    - budgets defined for every Mode
    - every task tag and candidate claim key is in the taxonomy
    """
    root = Path(root).resolve()
    problems: list[str] = []

    def each(sub: str, build, pattern: str = "*.toml") -> dict:
        out = {}
        for path in sorted((root / sub).glob(pattern)):
            try:
                out[path.stem] = build(path.stem, _toml(path, problems))
            except (KeyError, ValueError, TypeError) as e:
                problems.append(f"{path}: {type(e).__name__}: {e}")
        return out

    tax = _toml(root / "taxonomy.toml", problems)
    taxonomy, tax_version = tuple(tax.get("tags", ())), str(tax.get("version", ""))
    for tag in taxonomy:
        if ":" not in tag:
            problems.append(f"taxonomy tag {tag!r} is not axis:category")
    candidates = each("candidates", _candidate)
    configs = each("configs", _config)
    packages: dict[str, BenchmarkPackage] = {}
    for pdir in sorted(x for x in (root / "packages").glob("*/*") if x.is_dir()):
        try:
            pkg = _package(pdir, problems)
            packages[str(pkg.ref)] = pkg
        except (KeyError, ValueError, TypeError) as e:
            problems.append(f"{pdir}: {type(e).__name__}: {e}")

    for cid, c in configs.items():
        if c.method_id not in candidates:
            problems.append(f"config {cid}: unknown method_id {c.method_id!r}")
        files = [root.parent / a for a in c.adapter.command if "/" in a]
        problems += [f"config {cid}: adapter_command file {f} not found" for f in files if not f.is_file()]
        h = hashlib.sha256(b"".join(f.read_bytes() for f in files if f.is_file())).hexdigest()[:16]
        configs[cid] = replace(c, adapter=replace(c.adapter, digest=h))
    for mid, cand in candidates.items():
        problems += [f"candidate {mid}: claim tag {k!r} not in taxonomy" for k, _ in cand.claims if k not in taxonomy]
    for key, pkg in packages.items():
        if pkg.taxonomy_version != tax_version:
            problems.append(f"package {key}: taxonomy_version {pkg.taxonomy_version!r} != catalog {tax_version!r}")
        problems += [f"package {key}: task {t.task_id}: tag {g!r} not in taxonomy"
                     for t in pkg.tasks for g in sorted(t.tags) if g not in taxonomy]

    lock_path = root / "packages.lock"
    lock = json.loads(lock_path.read_text("utf-8")) if lock_path.exists() else {}
    for key, pkg in packages.items():
        if lock.get(key, pkg.ref.content_hash) != pkg.ref.content_hash:
            problems.append(f"package {key}: content changed since it was locked ({lock[key]} -> {pkg.ref.content_hash})")
    if problems:
        raise CatalogError(problems)
    new = {k: p.ref.content_hash for k, p in packages.items() if k not in lock}
    if new:
        lock_path.write_text(json.dumps({**lock, **new}, sort_keys=True, indent=1) + "\n", "utf-8")
    return Catalog(root, tax_version, taxonomy, candidates, configs, packages)

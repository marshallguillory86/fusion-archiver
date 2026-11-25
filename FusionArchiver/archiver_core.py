"""FusionArchiver core logic.

Pure Python (no Fusion imports) so it can be unit tested outside Fusion.
The Fusion-specific adapter and settings dialog live in FusionArchiver.py.

Behaviour:
  * Latest version of every document only.
  * Models   -> the selected model formats (default .f3d + .step).
  * Drawings -> .pdf (optional, can be deferred to a later run).
  * Models classified as imported can go to a separate Imported/ tree.
  * Non-design files are skipped but recorded in the inventory.
  * Every document gets one inventory.csv row with the evidence used to
    classify it, so the classification can be reviewed and corrected.
  * Resumable: state.json records finished documents by id + version.
"""
from __future__ import annotations

import csv
import fnmatch
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

VERSION = "1.1.0"

DESIGN_EXTS = frozenset({"f3d"})
DRAWING_EXTS = frozenset({"f2d"})
# Uploaded foreign CAD/mesh files that Fusion opens as designs.
IMPORT_EXTS = frozenset({
    "step", "stp", "iges", "igs", "sat", "smt", "stl", "obj", "3mf",
    "x_t", "x_b", "sldprt", "sldasm", "ipt", "iam", "prt", "catpart",
    "dwg", "dxf", "skp", "fbx", "3dm", "f3z",
})

CATEGORY_DESIGN = "design"
CATEGORY_DRAWING = "drawing"
CATEGORY_IMPORTED = "imported"
CATEGORY_SKIPPED = "skipped_non_design"

# Order here is the order shown in the settings dialog.
MODEL_FORMATS = ("f3d", "step", "iges", "sat", "smt", "stl", "3mf")
DRAWING_FORMATS = ("pdf",)
FORMAT_LABELS = {
    "f3d": "Fusion archive (.f3d / .f3z)",
    "step": "STEP (.step)",
    "iges": "IGES (.iges)",
    "sat": "ACIS (.sat)",
    "smt": "SMT (.smt)",
    "stl": "STL mesh (.stl)",
    "3mf": "3MF mesh (.3mf)",
    "pdf": "Drawings to PDF (.pdf)",
}

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME = 120


class Cancelled(Exception):
    """Raised by a Source while listing when the user presses Stop."""


class ConfigError(ValueError):
    """Invalid settings."""


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FileRef:
    """Cloud metadata for one document; cheap to obtain (no open)."""
    file_id: str
    project: str
    folder_parts: Tuple[str, ...]
    name: str
    extension: str
    version: int
    created_by: str = ""
    handle: object = field(default=None, compare=False, repr=False)

    @property
    def ext(self) -> str:
        return (self.extension or "").lower().lstrip(".")


@dataclass
class Evidence:
    """Facts read from an opened design, used only for classification."""
    design_type: str = ""        # "parametric" | "direct" | ""
    timeline_count: int = -1     # -1 = unknown
    base_feature_count: int = 0
    sketch_count: int = 0
    brep_body_count: int = 0
    mesh_body_count: int = 0


@dataclass
class Config:
    """All user-facing options. Persisted as JSON by the settings dialog."""
    output_folder: str = ""
    model_formats: List[str] = field(default_factory=lambda: ["f3d", "step"])
    drawing_formats: List[str] = field(default_factory=lambda: ["pdf"])
    defer_drawings: bool = False
    separate_imported: bool = True
    mine_patterns: List[str] = field(default_factory=list)
    imported_patterns: List[str] = field(default_factory=list)
    skip_projects: List[str] = field(default_factory=list)

    _LIST_FIELDS = ("model_formats", "drawing_formats", "mine_patterns",
                    "imported_patterns", "skip_projects")
    _BOOL_FIELDS = ("defer_drawings", "separate_imported")

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("settings must be a JSON object")
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key not in known:
                continue  # ignore unknown keys (forward compatibility)
            if key in cls._LIST_FIELDS:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ConfigError(f"'{key}' must be a list of strings")
                value = [v.strip() for v in value if v.strip()]
            elif key in cls._BOOL_FIELDS:
                if not isinstance(value, bool):
                    raise ConfigError(f"'{key}' must be true or false")
            elif key == "output_folder":
                if not isinstance(value, str):
                    raise ConfigError("'output_folder' must be a string")
            setattr(cfg, key, value)
        cfg.validate(require_output=False)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def validate(self, require_output: bool = True) -> None:
        bad = [f for f in self.model_formats if f not in MODEL_FORMATS]
        if bad:
            raise ConfigError(f"unknown model format(s): {', '.join(bad)}")
        bad = [f for f in self.drawing_formats if f not in DRAWING_FORMATS]
        if bad:
            raise ConfigError(f"unknown drawing format(s): {', '.join(bad)}")
        if not self.model_formats and not self.drawing_formats:
            raise ConfigError("select at least one export format")
        if require_output and not self.output_folder.strip():
            raise ConfigError("choose an output folder")

    @classmethod
    def load(cls, path: str) -> Config:
        if not os.path.isfile(path):
            return cls()
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def save(self, path: str) -> None:
        _atomic_write_json(path, self.to_dict())


def split_lines(text: str) -> List[str]:
    """Split a ';'- or newline-separated field into trimmed, non-empty entries."""
    return [part.strip() for part in re.split(r"[;\r\n]+", text or "") if part.strip()]


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _matches(name: str, patterns: Sequence[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns)


def kind_of(ref: FileRef) -> str:
    if ref.ext in DESIGN_EXTS or ref.ext in IMPORT_EXTS:
        return "model"
    if ref.ext in DRAWING_EXTS:
        return "drawing"
    return "other"


def classify(ref: FileRef, ev: Optional[Evidence], cfg: Config) -> Tuple[str, str]:
    """Return (category, reason). Order of rules is deliberate."""
    kind = kind_of(ref)
    if kind == "other":
        return CATEGORY_SKIPPED, f"non-design file type '.{ref.ext or '?'}'"
    if kind == "drawing":
        return CATEGORY_DRAWING, "drawing"
    if _matches(ref.name, cfg.mine_patterns):
        return CATEGORY_DESIGN, "settings: treat as mine"
    if _matches(ref.name, cfg.imported_patterns):
        return CATEGORY_IMPORTED, "settings: treat as imported"
    if ref.ext in IMPORT_EXTS:
        return CATEGORY_IMPORTED, f"uploaded as .{ref.ext}"
    if ev is None:
        return CATEGORY_DESIGN, "no evidence (could not open); defaulted to design"
    if ev.sketch_count > 0:
        return CATEGORY_DESIGN, f"{ev.sketch_count} sketch(es)"
    if ev.mesh_body_count > 0 and ev.brep_body_count == 0:
        return CATEGORY_IMPORTED, "mesh bodies only, no sketches"
    no_history = (
        ev.design_type == "direct"
        or ev.timeline_count == 0
        or (ev.timeline_count > 0 and ev.timeline_count == ev.base_feature_count)
    )
    if no_history:
        return CATEGORY_IMPORTED, "no sketches and no parametric history"
    return CATEGORY_DESIGN, "parametric history present"


def formats_for(category: str, cfg: Config) -> Tuple[str, ...]:
    if category == CATEGORY_DRAWING:
        return tuple(cfg.drawing_formats)
    if category in (CATEGORY_DESIGN, CATEGORY_IMPORTED):
        return tuple(cfg.model_formats)
    return ()


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def sanitize(name: str) -> str:
    cleaned = _BAD_CHARS.sub(" ", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "unnamed")[:_MAX_NAME]


def target_dir(out_root: str, ref: FileRef, category: str, cfg: Optional[Config] = None) -> str:
    separate = True if cfg is None else cfg.separate_imported
    top = "Imported" if (category == CATEGORY_IMPORTED and separate) else "Designs"
    parts = [out_root, top, sanitize(ref.project)] + [sanitize(p) for p in ref.folder_parts]
    return os.path.join(*parts)


def target_path(out_root: str, ref: FileRef, category: str, fmt: str, disambiguate: bool,
                cfg: Optional[Config] = None) -> str:
    stem = sanitize(ref.name)
    if disambiguate:
        stem = f"{stem}__{sanitize(ref.file_id)[-8:]}"
    return os.path.join(target_dir(out_root, ref, category, cfg), f"{stem}.{fmt}")


# --------------------------------------------------------------------------
# State (resume) + inventory
# --------------------------------------------------------------------------
def _atomic_write_json(path: str, data: Any) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class State:
    """Persists finished documents and which doc owns each output stem."""

    def __init__(self, path: str):
        self.path = path
        self.done: dict = {}
        self.owners: dict = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self.done = raw.get("done", {})
            self.owners = raw.get("owners", {})

    def is_done(self, ref: FileRef) -> bool:
        rec = self.done.get(ref.file_id)
        return bool(rec) and rec.get("version") == ref.version and rec.get("status") == "ok"

    def claim(self, stem_key: str, file_id: str) -> bool:
        """True if file_id owns stem_key (claims it if free)."""
        owner = self.owners.setdefault(stem_key, file_id)
        return owner == file_id

    def mark(self, ref: FileRef, status: str) -> None:
        self.done[ref.file_id] = {"version": ref.version, "status": status}
        self.save()

    def save(self) -> None:
        _atomic_write_json(self.path, {"version": VERSION, "done": self.done, "owners": self.owners})


INVENTORY_FIELDS = (
    "project", "folder", "name", "extension", "version", "created_by",
    "category", "reason", "design_type", "timeline_count", "base_feature_count",
    "sketch_count", "brep_body_count", "mesh_body_count",
    "status", "outputs", "errors", "file_id",
)


class Inventory:
    def __init__(self, path: str):
        self.path = path
        new = not os.path.isfile(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=INVENTORY_FIELDS)
        if new:
            self._w.writeheader()
            self._fh.flush()

    def write(self, ref: FileRef, category: str, reason: str, ev: Optional[Evidence],
              status: str, outputs: Iterable[str], errors: Iterable[str]) -> None:
        row = {
            "project": ref.project, "folder": "/".join(ref.folder_parts), "name": ref.name,
            "extension": ref.ext, "version": ref.version, "created_by": ref.created_by,
            "category": category, "reason": reason, "status": status,
            "outputs": " | ".join(outputs), "errors": " | ".join(errors), "file_id": ref.file_id,
        }
        row.update(asdict(ev or Evidence()))
        if ev is None:
            row.update({"timeline_count": "", "design_type": ""})
        self._w.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
class OpenedDoc(Protocol):
    def evidence(self) -> Evidence: ...

    def export(self, fmt: str, path: str) -> Optional[str]:
        """Export; return the path actually written (may differ, e.g. .f3z)."""
        ...

    def close(self) -> None: ...


class Source(Protocol):
    def iter_files(self) -> Iterable[FileRef]: ...

    def open(self, ref: FileRef) -> OpenedDoc: ...


@dataclass
class Summary:
    total: int = 0
    exported: int = 0
    skipped_done: int = 0
    skipped_non_design: int = 0
    partial: int = 0
    failed_open: int = 0
    deferred: int = 0
    cancelled: bool = False
    by_category: dict = field(default_factory=dict)


def run(source: Source, out_root: str, cfg: Config,
        log: Callable[[str], None] = lambda m: None,
        progress: Callable[[int, int, str], bool] = lambda i, n, m: True) -> Summary:
    """Archive every document. progress() returns False to cancel."""
    cfg.validate(require_output=False)
    state = State(os.path.join(out_root, "state.json"))
    inv = Inventory(os.path.join(out_root, "inventory.csv"))
    summary = Summary()
    try:
        try:
            refs = [r for r in source.iter_files() if r.project not in cfg.skip_projects]
        except Cancelled:
            summary.cancelled = True
            log("Cancelled by user while listing")
            return summary
        summary.total = len(refs)
        log(f"FusionArchiver {VERSION}: {len(refs)} documents found")
        for i, ref in enumerate(refs):
            if not progress(i, len(refs), f"{ref.project} / {ref.name}"):
                summary.cancelled = True
                log("Cancelled by user")
                break
            if state.is_done(ref):
                summary.skipped_done += 1
                continue
            _process(source, ref, out_root, cfg, state, inv, summary, log)
    finally:
        inv.close()
        state.save()
    log(f"Done: {asdict(summary)}")
    return summary


def _process(source, ref, out_root, cfg, state, inv, summary, log) -> None:
    kind = kind_of(ref)
    if kind == "drawing" and (cfg.defer_drawings or not cfg.drawing_formats):
        reason = "drawing (deferred)" if cfg.defer_drawings else "drawing (no drawing format selected)"
        inv.write(ref, CATEGORY_DRAWING, reason, None, "deferred", [], [])
        state.mark(ref, "deferred")
        summary.deferred += 1
        return
    if kind == "model" and not cfg.model_formats:
        inv.write(ref, CATEGORY_DESIGN, "model (no model format selected)", None, "deferred", [], [])
        state.mark(ref, "deferred")
        summary.deferred += 1
        return
    if kind == "other":
        category, reason = classify(ref, None, cfg)
        inv.write(ref, category, reason, None, "skipped", [], [])
        state.mark(ref, "ok")
        summary.skipped_non_design += 1
        return

    try:
        doc = source.open(ref)
    except Exception as exc:  # noqa: BLE001 - record and continue
        category, reason = classify(ref, None, cfg)
        inv.write(ref, category, reason, None, "open_failed", [], [f"open: {exc}"])
        state.mark(ref, "open_failed")
        summary.failed_open += 1
        log(f"OPEN FAILED {ref.name}: {exc}")
        return

    outputs: List[str] = []
    errors: List[str] = []
    ev: Optional[Evidence] = None
    category, reason = CATEGORY_DESIGN, ""
    try:
        if kind == "model":
            try:
                ev = doc.evidence()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"evidence: {exc}")
        category, reason = classify(ref, ev, cfg)
        stem_key = os.path.join(target_dir(out_root, ref, category, cfg), sanitize(ref.name)).lower()
        disambiguate = not state.claim(stem_key, ref.file_id)
        for fmt in formats_for(category, cfg):
            path = target_path(out_root, ref, category, fmt, disambiguate, cfg)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                written = doc.export(fmt, path) or path
                outputs.append(os.path.relpath(written, out_root))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fmt}: {exc}")
    finally:
        try:
            doc.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"close: {exc}")

    status = "ok" if not errors and outputs else ("partial" if outputs else "failed")
    inv.write(ref, category, reason, ev, status, outputs, errors)
    state.mark(ref, status)
    summary.by_category[category] = summary.by_category.get(category, 0) + 1
    if status == "ok":
        summary.exported += 1
    else:
        summary.partial += 1
    log(f"[{category}] {ref.project}/{'/'.join(ref.folder_parts)}/{ref.name} v{ref.version} -> {status}"
        + (f" ({'; '.join(errors)})" if errors else ""))

"""Tests for the Fusion adapter using a stub `adsk` package (Fusion is not available in CI)."""
import importlib
import json
import os
import sys
import types

import pytest

import archiver_core as core


class _Handler:
    def __init__(self, *a, **k):
        pass


def _install_stub_adsk(monkeypatch):
    adsk = types.ModuleType("adsk")
    adsk.doEvents = lambda: None
    adsk.terminate = lambda: None
    adsk.autoTerminate = lambda v: None
    adsk_core = types.ModuleType("adsk.core")
    for name in ("CommandCreatedEventHandler", "InputChangedEventHandler", "ValidateInputsEventHandler",
                 "CommandEventHandler", "CustomEventHandler"):
        setattr(adsk_core, name, _Handler)
    adsk_core.Application = types.SimpleNamespace(get=lambda: None)
    adsk_core.DialogResults = types.SimpleNamespace(DialogOK=0)
    adsk_fusion = types.ModuleType("adsk.fusion")
    adsk_fusion.Design = types.SimpleNamespace(cast=lambda x: x)
    adsk_fusion.DesignTypes = types.SimpleNamespace(DirectDesignType=0, ParametricDesignType=1)
    adsk_fusion.BaseFeature = types.SimpleNamespace(classType=lambda: "adsk::fusion::BaseFeature")
    adsk_drawing = types.ModuleType("adsk.drawing")
    adsk_drawing.Drawing = types.SimpleNamespace(cast=lambda x: x)
    adsk.core, adsk.fusion, adsk.drawing = adsk_core, adsk_fusion, adsk_drawing
    for name, mod in (("adsk", adsk), ("adsk.core", adsk_core), ("adsk.fusion", adsk_fusion),
                      ("adsk.drawing", adsk_drawing)):
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def fa(monkeypatch):
    _install_stub_adsk(monkeypatch)
    sys.modules.pop("FusionArchiver", None)
    module = importlib.import_module("FusionArchiver")
    yield module
    sys.modules.pop("FusionArchiver", None)


# ---- export dispatch ------------------------------------------------------
class RecordingExportManager:
    """Records create*Options calls; execute() writes the file the options name (like Fusion)."""

    def __init__(self, results=None, write=True, append_ext=False):
        self.calls = []
        self.results = list(results or [])
        self.write = write
        self.append_ext = append_ext

    def __getattr__(self, name):
        if name.startswith("create"):
            def factory(*args):
                self.calls.append((name, args))
                path = next(a for a in args if isinstance(a, str))
                return {"path": path, "name": name}
            return factory
        raise AttributeError(name)

    def execute(self, opts):
        if self.write:
            path = opts["path"]
            if self.append_ext:
                path = path + ".f3d"
            with open(path, "wb") as fh:
                fh.write(b"PK")
        return self.results.pop(0) if self.results else True


def _design(em=None):
    root = types.SimpleNamespace(name="root")
    return types.SimpleNamespace(exportManager=em or RecordingExportManager(), rootComponent=root)


def _doc_with(design):
    products = types.SimpleNamespace(itemByProductType=lambda t: design)
    return types.SimpleNamespace(products=products)


@pytest.mark.parametrize("fmt,method,path_index", [
    ("f3d", "createFusionArchiveExportOptions", 0),
    ("step", "createSTEPExportOptions", 0),
    ("iges", "createIGESExportOptions", 0),
    ("sat", "createSATExportOptions", 0),
    ("smt", "createSMTExportOptions", 0),
    ("stl", "createSTLExportOptions", 1),   # Fusion signature: (geometry, filename)
    ("3mf", "createC3MFExportOptions", 1),  # Fusion signature: (geometry, filename)
])
def test_each_model_format_uses_right_fusion_call(fa, tmp_path, fmt, method, path_index):
    design = _design()
    target = str(tmp_path / f"x.{fmt}")
    written = fa.FusionDoc(None, _doc_with(design)).export(fmt, target)
    name, args = design.exportManager.calls[0]
    assert name == method
    assert args[path_index] == target
    assert args[1 - path_index] is design.rootComponent
    assert written == target


def test_every_offered_model_format_is_implemented(fa, tmp_path):
    for fmt in core.MODEL_FORMATS:
        fa.FusionDoc(None, _doc_with(_design())).export(fmt, str(tmp_path / f"o.{fmt}"))


def test_archive_is_always_f3d(fa, tmp_path):
    written = fa.FusionDoc(None, _doc_with(_design())).export("f3d", str(tmp_path / "asm.f3d"))
    assert written.endswith("asm.f3d") and os.listdir(tmp_path) == ["asm.f3d"]


def test_extension_appended_by_fusion_is_normalised(fa, tmp_path):
    design = _design(RecordingExportManager(append_ext=True))
    written = fa.FusionDoc(None, _doc_with(design)).export("f3d", str(tmp_path / "asm.f3d"))
    assert os.listdir(tmp_path) == ["asm.f3d"] and written == str(tmp_path / "asm.f3d")


def test_false_but_file_written_is_success_and_logged(fa, tmp_path):
    logs = []
    design = _design(RecordingExportManager(results=[False]))
    written = fa.FusionDoc(None, _doc_with(design), logs.append).export("f3d", str(tmp_path / "rack.f3d"))
    assert written == str(tmp_path / "rack.f3d")
    assert any("returned False" in m for m in logs)


@pytest.mark.parametrize("result", [True, False])
def test_no_file_written_raises(fa, tmp_path, result):
    design = _design(RecordingExportManager(results=[result], write=False))
    with pytest.raises(RuntimeError, match="wrote no file"):
        fa.FusionDoc(None, _doc_with(design)).export("step", str(tmp_path / "x.step"))


def test_unknown_format_rejected(fa, tmp_path):
    with pytest.raises(ValueError):
        fa.FusionDoc(None, _doc_with(_design())).export("dwg", str(tmp_path / "o.dwg"))


# ---- settings dialog ------------------------------------------------------
class FakeInputs:
    def __init__(self, values):
        self._values = values

    def itemById(self, input_id):  # noqa: N802 - Fusion API name
        return types.SimpleNamespace(value=self._values[input_id])


def _dialog_values(**overrides):
    values = {f"fmt_{f}": False for f in core.MODEL_FORMATS + core.DRAWING_FORMATS}
    values.update({"output_folder": " /Volumes/Drive/Archive ", "defer_drawings": True,
                   "separate_imported": False, "download_originals": True, "mine_patterns": "hull_*; logo*",
                   "imported_patterns": "", "skip_projects": "Old;Scratch"})
    values.update(overrides)
    return values


def test_read_dialog_builds_config(fa):
    cfg = fa._read_dialog(FakeInputs(_dialog_values(fmt_step=True, fmt_stl=True, fmt_pdf=True)))
    assert cfg == core.Config(output_folder="/Volumes/Drive/Archive", model_formats=["step", "stl"],
                              drawing_formats=["pdf"], defer_drawings=True, separate_imported=False,
                              download_originals=True,
                              mine_patterns=["hull_*", "logo*"], imported_patterns=[],
                              skip_projects=["Old", "Scratch"])


def test_read_dialog_nothing_selected_fails_validation(fa):
    with pytest.raises(core.ConfigError):
        fa._read_dialog(FakeInputs(_dialog_values(download_originals=False))).validate()


def test_download_calls_datafile_download_synchronously(fa):
    calls = []
    handle = types.SimpleNamespace(download=lambda path, handler: calls.append((path, handler)) or True)
    r = core.FileRef(file_id="x", project="P", folder_parts=(), name="a.dxf", extension="dxf", version=1,
                     handle=handle)
    fa.FusionSource(None, lambda m: None).download(r, "/out/a.dxf")
    assert calls == [("/out/a.dxf", None)]


def test_download_false_raises(fa):
    handle = types.SimpleNamespace(download=lambda path, handler: False)
    r = core.FileRef(file_id="x", project="P", folder_parts=(), name="a", extension="pdf", version=1, handle=handle)
    with pytest.raises(RuntimeError):
        fa.FusionSource(None, lambda m: None).download(r, "/out/a.pdf")


def test_load_settings_prefers_settings_then_legacy(fa, tmp_path, monkeypatch):
    settings, legacy = tmp_path / "settings.json", tmp_path / "archiver_config.json"
    monkeypatch.setattr(fa, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(fa, "LEGACY_CONFIG_PATH", str(legacy))
    assert fa.load_settings() == core.Config()
    legacy.write_text(json.dumps({"mine_patterns": ["a*"], "defer_drawings": True}))
    assert fa.load_settings().mine_patterns == ["a*"]
    settings.write_text(json.dumps({"mine_patterns": ["b*"]}))
    assert fa.load_settings().mine_patterns == ["b*"]


def test_load_settings_skips_corrupt_file(fa, tmp_path, monkeypatch):
    settings, legacy = tmp_path / "settings.json", tmp_path / "archiver_config.json"
    monkeypatch.setattr(fa, "SETTINGS_PATH", str(settings))
    monkeypatch.setattr(fa, "LEGACY_CONFIG_PATH", str(legacy))
    settings.write_text("{not json")
    legacy.write_text(json.dumps({"model_formats": ["stl"]}))
    assert fa.load_settings().model_formats == ["stl"]


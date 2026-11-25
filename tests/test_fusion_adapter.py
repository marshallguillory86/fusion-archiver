"""Tests for the Fusion adapter using a stub `adsk` package (Fusion is not available in CI)."""
import importlib
import json
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
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("create"):
            def factory(*args):
                self.calls.append((name, args))
                return (name, args)
            return factory
        raise AttributeError(name)

    def execute(self, opts):
        return True


class Occs:
    def __init__(self, refs):
        self._refs = refs
        self.count = len(refs)

    def item(self, i):
        return types.SimpleNamespace(isReferencedComponent=self._refs[i])


def _design(refs=()):
    root = types.SimpleNamespace(allOccurrences=Occs(list(refs)))
    return types.SimpleNamespace(exportManager=RecordingExportManager(), rootComponent=root)


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
def test_each_model_format_uses_right_fusion_call(fa, fmt, method, path_index):
    design = _design()
    doc = fa.FusionDoc(None, _doc_with(design))
    written = doc.export(fmt, f"/out/x.{fmt}")
    name, args = design.exportManager.calls[0]
    assert name == method
    assert args[path_index] == f"/out/x.{fmt}"
    assert args[1 - path_index] is design.rootComponent
    assert written == f"/out/x.{fmt}"


def test_every_offered_model_format_is_implemented(fa):
    for fmt in core.MODEL_FORMATS:
        fa.FusionDoc(None, _doc_with(_design())).export(fmt, f"/o.{fmt}")


def test_f3d_with_external_refs_becomes_f3z(fa):
    design = _design(refs=[False, True])
    written = fa.FusionDoc(None, _doc_with(design)).export("f3d", "/out/asm.f3d")
    assert written == "/out/asm.f3z"


def test_unknown_format_rejected(fa):
    with pytest.raises(ValueError):
        fa.FusionDoc(None, _doc_with(_design())).export("dwg", "/o.dwg")


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


class FailingFirstExportManager(RecordingExportManager):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def execute(self, opts):
        return self.results.pop(0)


def test_f3d_false_retries_as_f3z(fa):
    design = _design()
    design.exportManager = FailingFirstExportManager([False, True])
    written = fa.FusionDoc(None, _doc_with(design)).export("f3d", "/out/rack.f3d")
    assert written == "/out/rack.f3z"
    assert [c[1][0] for c in design.exportManager.calls] == ["/out/rack.f3d", "/out/rack.f3z"]


def test_f3d_and_f3z_both_false_raises(fa):
    design = _design()
    design.exportManager = FailingFirstExportManager([False, False])
    with pytest.raises(RuntimeError, match="f3d and f3z"):
        fa.FusionDoc(None, _doc_with(design)).export("f3d", "/out/rack.f3d")


def test_detected_f3z_false_raises_without_retry(fa):
    design = _design(refs=[True])
    design.exportManager = FailingFirstExportManager([False])
    with pytest.raises(RuntimeError, match="f3z export returned False"):
        fa.FusionDoc(None, _doc_with(design)).export("f3d", "/out/asm.f3d")

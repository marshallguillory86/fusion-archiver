"""Unit tests for archiver_core."""
import json
import os

import pytest

import archiver_core as core
from archiver_core import Config, ConfigError, Evidence, FileRef


def ref(name="Part", ext="f3d", fid="urn:abc12345", project="P", parts=(), version=3):
    return FileRef(file_id=fid, project=project, folder_parts=tuple(parts), name=name,
                   extension=ext, version=version)


CFG = Config()


class TestClassify:
    def test_non_design_skipped(self):
        assert core.classify(ref(ext="htm"), None, CFG)[0] == core.CATEGORY_SKIPPED

    def test_empty_extension_skipped(self):
        assert core.classify(ref(ext=""), None, CFG)[0] == core.CATEGORY_SKIPPED

    def test_drawing(self):
        assert core.classify(ref(ext="f2d"), None, CFG)[0] == core.CATEGORY_DRAWING

    def test_uploaded_step_is_imported(self):
        cat, reason = core.classify(ref(ext="STP"), Evidence(sketch_count=4), CFG)
        assert cat == core.CATEGORY_IMPORTED and ".stp" in reason

    def test_sketches_mean_design(self):
        ev = Evidence(design_type="parametric", timeline_count=5, sketch_count=2)
        assert core.classify(ref(), ev, CFG)[0] == core.CATEGORY_DESIGN

    def test_direct_no_sketches_imported(self):
        ev = Evidence(design_type="direct", timeline_count=0, brep_body_count=3)
        assert core.classify(ref(), ev, CFG)[0] == core.CATEGORY_IMPORTED

    def test_only_base_features_imported(self):
        ev = Evidence(design_type="parametric", timeline_count=2, base_feature_count=2, brep_body_count=1)
        assert core.classify(ref(), ev, CFG)[0] == core.CATEGORY_IMPORTED

    def test_mesh_only_imported(self):
        ev = Evidence(design_type="parametric", timeline_count=3, mesh_body_count=1)
        assert core.classify(ref(), ev, CFG)[1] == "mesh bodies only, no sketches"

    def test_parametric_history_without_sketches_is_design(self):
        ev = Evidence(design_type="parametric", timeline_count=4, base_feature_count=1, brep_body_count=1)
        assert core.classify(ref(), ev, CFG)[0] == core.CATEGORY_DESIGN

    def test_no_evidence_defaults_design(self):
        assert core.classify(ref(), None, CFG)[0] == core.CATEGORY_DESIGN

    def test_mine_pattern_overrides_heuristic(self):
        cfg = Config(mine_patterns=["hull_*"])
        cat, reason = core.classify(ref(name="hull_v0.7.3"), Evidence(design_type="direct", timeline_count=0), cfg)
        assert (cat, reason) == (core.CATEGORY_DESIGN, "settings: treat as mine")

    def test_mine_pattern_case_insensitive(self):
        cfg = Config(mine_patterns=["hull_*"])
        assert core.classify(ref(name="HULL_v1"), Evidence(design_type="direct"), cfg)[0] == core.CATEGORY_DESIGN

    def test_imported_pattern_beats_sketches(self):
        cfg = Config(imported_patterns=["94425A*"])
        assert core.classify(ref(name="94425A810_Screw"), Evidence(sketch_count=9), cfg)[0] == core.CATEGORY_IMPORTED

    def test_mine_beats_imported_pattern(self):
        cfg = Config(mine_patterns=["X*"], imported_patterns=["X*"])
        assert core.classify(ref(name="X1"), None, cfg)[0] == core.CATEGORY_DESIGN


class TestFormats:
    def test_defaults(self):
        assert core.formats_for(core.CATEGORY_DESIGN, CFG) == ("f3d", "step")
        assert core.formats_for(core.CATEGORY_IMPORTED, CFG) == ("f3d", "step")
        assert core.formats_for(core.CATEGORY_DRAWING, CFG) == ("pdf",)
        assert core.formats_for(core.CATEGORY_SKIPPED, CFG) == ()

    def test_custom(self):
        cfg = Config(model_formats=["stl", "3mf"], drawing_formats=[])
        assert core.formats_for(core.CATEGORY_DESIGN, cfg) == ("stl", "3mf")
        assert core.formats_for(core.CATEGORY_DRAWING, cfg) == ()

    def test_every_format_has_label(self):
        assert set(core.FORMAT_LABELS) == set(core.MODEL_FORMATS + core.DRAWING_FORMATS)


class TestPaths:
    @pytest.mark.parametrize("raw,expected", [
        ("Component1:1", "Component1 1"),
        ("a/b\\c", "a b c"),
        ("  ..x..  ", "x"),
        ("", "unnamed"),
        ("a\x00b", "a b"),
    ])
    def test_sanitize(self, raw, expected):
        assert core.sanitize(raw) == expected

    def test_sanitize_truncates(self):
        assert len(core.sanitize("x" * 500)) == 120

    def test_no_traversal(self):
        p = core.target_path("/out", ref(name="../../etc", parts=("..", "x")), core.CATEGORY_DESIGN, "f3d", False)
        assert os.path.normpath(p).startswith("/out/Designs/P")

    def test_imported_root(self):
        p = core.target_path("/out", ref(parts=("Type D",)), core.CATEGORY_IMPORTED, "step", False)
        assert p == "/out/Imported/P/Type D/Part.step"

    def test_imported_not_separated(self):
        cfg = Config(separate_imported=False)
        p = core.target_path("/out", ref(), core.CATEGORY_IMPORTED, "step", False, cfg)
        assert p == "/out/Designs/P/Part.step"

    def test_disambiguate(self):
        p = core.target_path("/out", ref(fid="urn:xyz:ABCDEFGH"), core.CATEGORY_DESIGN, "f3d", True)
        assert p.endswith("Part__ABCDEFGH.f3d")


class TestConfig:
    def test_defaults_are_generic(self):
        c = Config()
        assert c.mine_patterns == [] and c.imported_patterns == [] and c.skip_projects == []
        assert c.model_formats == ["f3d", "step"] and c.drawing_formats == ["pdf"]
        assert c.defer_drawings is False and c.separate_imported is True

    def test_missing_file_defaults(self, tmp_path):
        assert Config.load(str(tmp_path / "nope.json")) == Config()

    def test_roundtrip(self, tmp_path):
        p = str(tmp_path / "s.json")
        c = Config(output_folder="/x", model_formats=["stl"], drawing_formats=[], defer_drawings=True,
                   separate_imported=False, mine_patterns=["a*"], imported_patterns=["b*"], skip_projects=["P"])
        c.save(p)
        assert Config.load(p) == c

    def test_legacy_file_loads(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"mine_patterns": ["x*"], "imported_patterns": [], "skip_projects": [],
                                 "defer_drawings": True}))
        c = Config.load(str(p))
        assert c.mine_patterns == ["x*"] and c.defer_drawings is True and c.model_formats == ["f3d", "step"]

    def test_unknown_keys_ignored(self):
        assert Config.from_dict({"future_option": 1}) == Config()

    @pytest.mark.parametrize("raw", [
        {"mine_patterns": "hull"},
        {"defer_drawings": "no"},
        {"model_formats": ["dwg"]},
        {"drawing_formats": ["dxf"]},
        {"model_formats": [], "drawing_formats": []},
        {"output_folder": 5},
        [],
    ])
    def test_rejects_bad(self, raw):
        with pytest.raises(ConfigError):
            Config.from_dict(raw)

    def test_validate_requires_output(self):
        with pytest.raises(ConfigError):
            Config().validate(require_output=True)
        Config(output_folder="/x").validate(require_output=True)

    @pytest.mark.parametrize("text,expected", [
        ("a*; b* ;;c", ["a*", "b*", "c"]),
        ("a\nb\r\nc", ["a", "b", "c"]),
        ("", []),
        (None, []),
    ])
    def test_split_lines(self, text, expected):
        assert core.split_lines(text) == expected


class TestState:
    def test_roundtrip_and_version_change(self, tmp_path):
        s = core.State(str(tmp_path / "state.json"))
        r = ref(version=3)
        s.mark(r, "ok")
        s2 = core.State(str(tmp_path / "state.json"))
        assert s2.is_done(r)
        assert not s2.is_done(ref(version=4))

    @pytest.mark.parametrize("status", ["partial", "failed", "open_failed", "deferred"])
    def test_not_ok_is_retried(self, tmp_path, status):
        s = core.State(str(tmp_path / "state.json"))
        s.mark(ref(), status)
        assert not s.is_done(ref())

    def test_claim(self, tmp_path):
        s = core.State(str(tmp_path / "s.json"))
        assert s.claim("k", "a") and s.claim("k", "a") and not s.claim("k", "b")

    def test_no_temp_files_left(self, tmp_path):
        s = core.State(str(tmp_path / "state.json"))
        s.mark(ref(), "ok")
        assert os.listdir(tmp_path) == ["state.json"]

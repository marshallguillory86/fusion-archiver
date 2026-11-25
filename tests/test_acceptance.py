"""Acceptance tests (Given/When/Then) for archiver_core.run against a fake Fusion hub."""
import csv
import os

from archiver_core import Config, Evidence, FileRef, run


class FakeDoc:
    def __init__(self, ev, fail=(), refs=False, log=None):
        self.ev, self.fail, self.refs, self.log = ev, set(fail), refs, log

    def evidence(self):
        if isinstance(self.ev, Exception):
            raise self.ev
        return self.ev

    def export(self, fmt, path):
        if fmt in self.fail:
            raise RuntimeError("invalid geometry")
        if fmt == "f3d" and self.refs:
            path = os.path.splitext(path)[0] + ".f3z"
        with open(path, "w") as fh:
            fh.write(fmt)
        return path

    def close(self):
        self.log.append("close")


class FakeSource:
    def __init__(self, items):
        self.items = items  # list of (FileRef, FakeDoc | Exception)
        self.opened = []
        self.closes = []
        self.downloaded = []
        self.fail_download = set()

    def iter_files(self):
        return [r for r, _ in self.items]

    def download(self, ref, path):
        self.downloaded.append(ref.name)
        if ref.name in self.fail_download:
            raise RuntimeError("Failed to download file.")
        with open(path, "w") as fh:
            fh.write("original")

    def open(self, ref):
        self.opened.append(ref.name)
        doc = dict((r.file_id, d) for r, d in self.items)[ref.file_id]
        if isinstance(doc, Exception):
            raise doc
        doc.log = self.closes
        return doc


def R(name, ext="f3d", fid=None, parts=("Folder",), project="Proj", version=1):
    return FileRef(file_id=fid or f"id-{name}", project=project, folder_parts=parts,
                   name=name, extension=ext, version=version)


def rows(out):
    with open(os.path.join(out, "inventory.csv"), newline="") as fh:
        return {r["name"]: r for r in csv.DictReader(fh)}


MINE = Evidence(design_type="parametric", timeline_count=6, sketch_count=3, brep_body_count=2)
IMPORTED = Evidence(design_type="direct", timeline_count=0, brep_body_count=5)


def test_given_mixed_hub_when_run_then_designs_and_imported_separated(tmp_path):
    out = str(tmp_path)
    src = FakeSource([
        (R("Drawer"), FakeDoc(MINE)),
        (R("94425A810_Screw"), FakeDoc(IMPORTED)),
        (R("hull_v0.7.3", parts=("Boat",)), FakeDoc(IMPORTED)),
        (R("Drawer Drawing", ext="f2d"), FakeDoc(None)),
        (R("NestReport", ext="htm"), FakeDoc(None)),
    ])
    s = run(src, out, Config(mine_patterns=["hull_*"]))
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Drawer.f3d")
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Drawer.step")
    assert os.path.isfile(f"{out}/Imported/Proj/Folder/94425A810_Screw.step")
    assert os.path.isfile(f"{out}/Designs/Proj/Boat/hull_v0.7.3.f3d")
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Drawer Drawing.pdf")
    assert "NestReport" not in src.opened
    r = rows(out)
    assert r["NestReport"]["category"] == "original"
    assert os.path.isfile(f"{out}/Originals/Proj/Folder/NestReport.htm")
    assert r["94425A810_Screw"]["category"] == "imported"
    assert s.exported == 4 and s.originals == 1
    assert len(src.closes) == 4  # every opened doc closed


def test_given_finished_run_when_rerun_then_nothing_reopened(tmp_path):
    out = str(tmp_path)
    items = [(R("A"), FakeDoc(MINE)), (R("B"), FakeDoc(MINE))]
    run(FakeSource(items), out, Config())
    src2 = FakeSource(items)
    s = run(src2, out, Config())
    assert src2.opened == [] and s.skipped_done == 2


def test_given_new_version_when_rerun_then_reexported(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("A", version=1), FakeDoc(MINE))]), out, Config())
    src2 = FakeSource([(R("A", version=2), FakeDoc(MINE))])
    run(src2, out, Config())
    assert src2.opened == ["A"]


def test_given_cancel_midway_when_rerun_then_resumes(tmp_path):
    out = str(tmp_path)
    items = [(R(n), FakeDoc(MINE)) for n in "ABCD"]
    s1 = run(FakeSource(items), out, Config(), progress=lambda i, n, m: i < 2)
    assert s1.cancelled and s1.exported == 2
    src2 = FakeSource(items)
    run(src2, out, Config())
    assert src2.opened == ["C", "D"]


def test_given_step_fails_when_run_then_partial_recorded_and_retried(tmp_path):
    out = str(tmp_path)
    items = [(R("Bad"), FakeDoc(MINE, fail=("step",)))]
    s = run(FakeSource(items), out, Config())
    r = rows(out)["Bad"]
    assert r["status"] == "partial" and "invalid geometry" in r["errors"] and s.partial == 1
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Bad.f3d")
    src2 = FakeSource([(R("Bad"), FakeDoc(MINE))])
    run(src2, out, Config())
    assert src2.opened == ["Bad"]  # partial is retried


def test_given_open_fails_when_run_then_recorded_and_continues(tmp_path):
    out = str(tmp_path)
    src = FakeSource([(R("X"), RuntimeError("timeout")), (R("Y"), FakeDoc(MINE))])
    s = run(src, out, Config())
    assert s.failed_open == 1 and s.exported == 1
    assert rows(out)["X"]["status"] == "open_failed"


def test_given_evidence_error_when_run_then_defaults_design_and_exports(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("E"), FakeDoc(RuntimeError("boom")))]), out, Config())
    r = rows(out)["E"]
    assert r["category"] == "design" and r["status"] == "partial" and "evidence" in r["errors"]


def test_given_external_refs_when_run_then_f3z_written(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("Asm"), FakeDoc(MINE, refs=True))]), out, Config())
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Asm.f3z")
    assert "Asm.f3z" in rows(out)["Asm"]["outputs"]


def test_given_same_name_two_docs_when_run_then_no_overwrite(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("Dup", fid="urn:1111AAAA"), FakeDoc(MINE)),
                    (R("Dup", fid="urn:2222BBBB"), FakeDoc(MINE))]), out, Config())
    files = sorted(os.listdir(f"{out}/Designs/Proj/Folder"))
    assert files == ["Dup.f3d", "Dup.step", "Dup__2222BBBB.f3d", "Dup__2222BBBB.step"]


def test_given_skip_project_when_run_then_not_listed(tmp_path):
    out = str(tmp_path)
    src = FakeSource([(R("A", project="Keep"), FakeDoc(MINE)), (R("B", project="Drop"), FakeDoc(MINE))])
    s = run(src, out, Config(skip_projects=["Drop"]))
    assert s.total == 1 and src.opened == ["A"]


class CancellingSource(FakeSource):
    def iter_files(self):
        yield self.items[0][0]
        from archiver_core import Cancelled
        raise Cancelled()


def test_given_stop_during_listing_when_run_then_cancelled_nothing_opened(tmp_path):
    src = CancellingSource([(R("A"), FakeDoc(MINE))])
    s = run(src, str(tmp_path), Config())
    assert s.cancelled and src.opened == [] and s.total == 0



def test_given_defer_drawings_when_run_then_drawings_not_opened_and_retried_later(tmp_path):
    out = str(tmp_path)
    items = [(R("D", ext="f2d"), FakeDoc(None)), (R("M"), FakeDoc(MINE))]
    src = FakeSource(items)
    s = run(src, out, Config(defer_drawings=True))
    assert src.opened == ["M"] and s.deferred == 1
    assert rows(out)["D"]["status"] == "deferred"
    src2 = FakeSource(items)
    run(src2, out, Config(defer_drawings=False))
    assert src2.opened == ["D"]
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/D.pdf")


def test_given_custom_formats_when_run_then_only_those_written(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("M"), FakeDoc(MINE))]), out, Config(model_formats=["stl", "3mf"]))
    assert sorted(os.listdir(f"{out}/Designs/Proj/Folder")) == ["M.3mf", "M.stl"]


def test_given_not_separated_when_run_then_imported_in_designs(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("Screw"), FakeDoc(IMPORTED))]), out, Config(separate_imported=False))
    assert os.path.isfile(f"{out}/Designs/Proj/Folder/Screw.step")
    assert not os.path.exists(f"{out}/Imported")
    assert rows(out)["Screw"]["category"] == "imported"


def test_given_no_drawing_format_when_run_then_drawings_deferred(tmp_path):
    out = str(tmp_path)
    src = FakeSource([(R("D", ext="f2d"), FakeDoc(None))])
    s = run(src, out, Config(drawing_formats=[]))
    assert src.opened == [] and s.deferred == 1


def test_given_drawings_only_when_run_then_models_deferred_and_retried(tmp_path):
    out = str(tmp_path)
    items = [(R("D", ext="f2d"), FakeDoc(None)), (R("M"), FakeDoc(MINE))]
    src = FakeSource(items)
    run(src, out, Config(model_formats=[]))
    assert src.opened == ["D"]
    src2 = FakeSource(items)
    run(src2, out, Config())
    assert src2.opened == ["M"]


def test_given_invalid_config_when_run_then_raises_before_writing(tmp_path):
    import pytest

    from archiver_core import ConfigError
    with pytest.raises(ConfigError):
        run(FakeSource([]), str(tmp_path), Config(model_formats=[], drawing_formats=[], download_originals=False))
    assert os.listdir(tmp_path) == []


def test_given_uploaded_dxf_when_run_then_downloaded_not_opened(tmp_path):
    out = str(tmp_path)
    src = FakeSource([(R("OG Latch.dxf", ext="dxf"), FakeDoc(None))])
    s = run(src, out, Config())
    assert src.opened == [] and src.downloaded == ["OG Latch.dxf"] and s.originals == 1
    assert os.listdir(f"{out}/Originals/Proj/Folder") == ["OG Latch.dxf"]


def test_given_file_skipped_by_older_version_when_run_then_downloaded(tmp_path):
    out = str(tmp_path)
    ref = R("Template", ext="art")
    from archiver_core import State
    State(os.path.join(out, "state.json")).mark(ref, "ok")  # how <=1.1.1 recorded skipped files
    src = FakeSource([(ref, FakeDoc(None))])
    run(src, out, Config())
    assert src.downloaded == ["Template"]
    src2 = FakeSource([(ref, FakeDoc(None))])
    s2 = run(src2, out, Config())
    assert src2.downloaded == [] and s2.skipped_done == 1


def test_given_download_off_when_run_then_skipped_and_retried_when_turned_on(tmp_path):
    out = str(tmp_path)
    items = [(R("Template", ext="art"), FakeDoc(None))]
    src = FakeSource(items)
    s = run(src, out, Config(download_originals=False))
    assert src.downloaded == [] and s.skipped_non_design == 1
    src2 = FakeSource(items)
    run(src2, out, Config())
    assert src2.downloaded == ["Template"]


def test_given_download_fails_when_run_then_recorded_and_retried(tmp_path):
    out = str(tmp_path)
    items = [(R("geese", ext="pdf"), FakeDoc(None))]
    src = FakeSource(items)
    src.fail_download = {"geese"}
    s = run(src, out, Config())
    assert s.partial == 1 and rows(out)["geese"]["status"] == "failed"
    src2 = FakeSource(items)
    run(src2, out, Config())
    assert src2.downloaded == ["geese"]


def test_given_same_name_originals_when_run_then_no_overwrite(tmp_path):
    out = str(tmp_path)
    run(FakeSource([(R("Logo.dxf", ext="dxf", fid="urn:1111AAAA"), FakeDoc(None)),
                    (R("Logo.dxf", ext="dxf", fid="urn:2222BBBB"), FakeDoc(None))]), out, Config())
    assert sorted(os.listdir(f"{out}/Originals/Proj/Folder")) == ["Logo.dxf", "Logo__2222BBBB.dxf"]

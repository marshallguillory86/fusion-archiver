"""FusionArchiver: bulk-export the latest version of every Autodesk Fusion document.

Run from Fusion: Utilities > Scripts and Add-Ins > Scripts > FusionArchiver > Run.
A settings dialog opens first; settings are remembered in settings.json next to
this script. The export is resumable: run again with the same output folder and
finished documents are skipped.
"""
import contextlib
import datetime
import importlib
import os
import sys
import time
import traceback

import adsk.core
import adsk.drawing
import adsk.fusion

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import archiver_core as core  # noqa: E402

importlib.reload(core)  # Fusion keeps modules loaded between runs

SETTINGS_PATH = os.path.join(_HERE, "settings.json")
LEGACY_CONFIG_PATH = os.path.join(_HERE, "archiver_config.json")
CMD_ID = "FusionArchiverSettings"
START_EVENT_ID = "FusionArchiverStart"

_handlers = []          # Fusion requires handlers to stay referenced
_pending = {"cfg": None, "started": False}


# --------------------------------------------------------------------------
# Fusion adapter
# --------------------------------------------------------------------------
class FusionDoc:
    def __init__(self, app, doc, log=lambda m: None):
        self._app = app
        self._doc = doc
        self._log = log

    def _design(self):
        return adsk.fusion.Design.cast(self._doc.products.itemByProductType("DesignProductType"))

    def _drawing(self):
        try:
            drawing = adsk.drawing.Drawing.cast(self._doc.products.itemByProductType("DrawingProductType"))
        except Exception:
            drawing = None
        if drawing is None:
            # Opened drawings may not expose the product by type; use the active product.
            self._doc.activate()
            adsk.doEvents()
            drawing = adsk.drawing.Drawing.cast(self._app.activeProduct)
        return drawing

    def evidence(self):
        design = self._design()
        ev = core.Evidence()
        if design is None:
            return ev
        ev.design_type = ("direct" if design.designType == adsk.fusion.DesignTypes.DirectDesignType
                          else "parametric")
        try:
            timeline = design.timeline
            ev.timeline_count = timeline.count
            base_type = adsk.fusion.BaseFeature.classType()
            for i in range(timeline.count):
                entity = timeline.item(i).entity
                if entity is not None and entity.objectType == base_type:
                    ev.base_feature_count += 1
        except Exception:  # direct designs have no usable timeline
            ev.timeline_count = 0 if ev.design_type == "direct" else -1
        comps = design.allComponents
        for i in range(comps.count):
            comp = comps.item(i)
            ev.sketch_count += comp.sketches.count
            ev.brep_body_count += comp.bRepBodies.count
            ev.mesh_body_count += comp.meshBodies.count
        return ev

    @staticmethod
    def _has_external_refs(design):
        occs = design.rootComponent.allOccurrences
        return any(occs.item(i).isReferencedComponent for i in range(occs.count))

    def export(self, fmt, path):
        if fmt == "pdf":
            t0 = time.monotonic()
            drawing = self._drawing()
            self._log(f"  timing: get drawing product {time.monotonic() - t0:.1f}s")
            if drawing is None:
                raise RuntimeError("not a drawing")
            opts = drawing.exportManager.createPDFExportOptions(path)
            opts.openPDF = False
            opts.useLineWeights = True
            t1 = time.monotonic()
            if not drawing.exportManager.execute(opts):
                raise RuntimeError("PDF export returned False")
            self._log(f"  timing: PDF export {time.monotonic() - t1:.1f}s")
            return path
        design = self._design()
        if design is None:
            raise RuntimeError("not a design")
        em = design.exportManager
        root = design.rootComponent
        if fmt == "f3d":
            if self._has_external_refs(design):
                path = os.path.splitext(path)[0] + ".f3z"
            opts = em.createFusionArchiveExportOptions(path, root)
            if em.execute(opts):
                return path
            if path.endswith(".f3z"):
                raise RuntimeError("f3z export returned False")
            # Some assemblies with references are not detected above; Fusion needs .f3z for them.
            path = os.path.splitext(path)[0] + ".f3z"
            self._log("  f3d export returned False; retrying as .f3z")
            opts = em.createFusionArchiveExportOptions(path, root)
            if not em.execute(opts):
                raise RuntimeError("f3d and f3z export returned False")
            return path
        elif fmt == "step":
            opts = em.createSTEPExportOptions(path, root)
        elif fmt == "iges":
            opts = em.createIGESExportOptions(path, root)
        elif fmt == "sat":
            opts = em.createSATExportOptions(path, root)
        elif fmt == "smt":
            opts = em.createSMTExportOptions(path, root)
        elif fmt == "stl":
            opts = em.createSTLExportOptions(root, path)
        elif fmt == "3mf":
            opts = em.createC3MFExportOptions(root, path)
        else:
            raise ValueError(f"unsupported format {fmt}")
        if not em.execute(opts):
            raise RuntimeError(f"{fmt} export returned False")
        return path

    def close(self):
        self._doc.close(False)
        adsk.doEvents()


class FusionSource:
    def __init__(self, app, log, tick=lambda msg: True):
        self._app = app
        self._log = log
        self._tick = tick  # returns False when the user pressed Stop

    def iter_files(self):
        hub = self._app.data.activeHub
        self._log(f"Hub: {hub.name}")
        projects = hub.dataProjects
        for p in range(projects.count):
            project = projects.item(p)
            self._log(f"Listing project {project.name}")
            if not self._tick(f"Listing project {p + 1} of {projects.count}: {project.name}"):
                raise core.Cancelled()
            yield from self._walk(project.name, project.rootFolder, ())

    def _walk(self, project_name, folder, parts):
        files = folder.dataFiles
        for i in range(files.count):
            df = files.item(i)
            adsk.doEvents()
            try:
                created_by = df.createdBy.displayName if df.createdBy else ""
            except Exception:
                created_by = ""
            yield core.FileRef(
                file_id=df.id, project=project_name, folder_parts=parts, name=df.name,
                extension=df.fileExtension or "", version=df.latestVersionNumber,
                created_by=created_by, handle=df)
        folders = folder.dataFolders
        for i in range(folders.count):
            sub = folders.item(i)
            if not self._tick(f"Listing {project_name} / {'/'.join(parts + (sub.name,))}"):
                raise core.Cancelled()
            yield from self._walk(project_name, sub, parts + (sub.name,))

    def download(self, ref, path):
        if not ref.handle.download(path, None):
            raise RuntimeError("DataFile.download returned False")
        adsk.doEvents()

    def open(self, ref):
        df = ref.handle
        latest = getattr(df, "latestVersion", None) or df
        t0 = time.monotonic()
        doc = self._app.documents.open(latest, True)
        if doc is None:
            raise RuntimeError("documents.open returned None")
        adsk.doEvents()
        if ref.ext == "f2d":
            self._log(f"  timing: open drawing {time.monotonic() - t0:.1f}s")
        return FusionDoc(self._app, doc, self._log)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def load_settings():
    for path in (SETTINGS_PATH, LEGACY_CONFIG_PATH):
        if os.path.isfile(path):
            try:
                return core.Config.load(path)
            except (core.ConfigError, ValueError, OSError):
                continue
    return core.Config()


def _read_dialog(inputs):
    def val(input_id):
        return inputs.itemById(input_id).value

    cfg = core.Config(
        output_folder=val("output_folder").strip(),
        model_formats=[f for f in core.MODEL_FORMATS if val(f"fmt_{f}")],
        drawing_formats=[f for f in core.DRAWING_FORMATS if val(f"fmt_{f}")],
        defer_drawings=val("defer_drawings"),
        separate_imported=val("separate_imported"),
        download_originals=val("download_originals"),
        mine_patterns=core.split_lines(val("mine_patterns")),
        imported_patterns=core.split_lines(val("imported_patterns")),
        skip_projects=core.split_lines(val("skip_projects")),
    )
    return cfg


# --------------------------------------------------------------------------
# Dialog handlers
# --------------------------------------------------------------------------
class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cfg = load_settings()
            cmd = args.command
            cmd.okButtonText = "Start export"
            cmd.setDialogMinimumSize(560, 600)
            cmd.setDialogInitialSize(640, 680)
            inputs = cmd.commandInputs

            inputs.addTextBoxCommandInput(
                "about", "", f"<b>FusionArchiver {core.VERSION}</b><br>Exports the latest version of "
                "every document in the active hub. Safe to stop and re-run.", 3, True)

            out = inputs.addGroupCommandInput("grp_out", "Output").children
            out.addStringValueInput("output_folder", "Folder", cfg.output_folder)
            browse = out.addBoolValueInput("browse", " ", False, "", False)
            browse.text = "Browse..."

            fmts = inputs.addGroupCommandInput("grp_fmt", "Formats").children
            for f in core.MODEL_FORMATS + core.DRAWING_FORMATS:
                selected = f in cfg.model_formats or f in cfg.drawing_formats
                fmts.addBoolValueInput(f"fmt_{f}", core.FORMAT_LABELS[f], True, "", selected)

            opts = inputs.addGroupCommandInput("grp_opts", "Options").children
            d = opts.addBoolValueInput("defer_drawings", "Defer drawings (export models first)",
                                       True, "", cfg.defer_drawings)
            d.tooltip = "Drawings can be slow. Deferred drawings are exported on a later run with this unchecked."
            s = opts.addBoolValueInput("separate_imported", "Put imported models in a separate Imported folder",
                                       True, "", cfg.separate_imported)
            s.tooltip = "Imported = uploaded CAD files, or designs with no sketches and no parametric history."
            o = opts.addBoolValueInput("download_originals", "Download non-Fusion files as-is (DXF, PDF, templates...)",
                                       True, "", cfg.download_originals)
            o.tooltip = "Saved under Originals/. Fusion can download these files but cannot convert them."

            cls = inputs.addGroupCommandInput("grp_cls", "Classification and scope")
            cls.isExpanded = False
            c = cls.children
            hint = "Separate entries with ; and use * as a wildcard, e.g. bracket_*; McMaster*"
            for input_id, label, values in (("mine_patterns", "Always mine", cfg.mine_patterns),
                                            ("imported_patterns", "Always imported", cfg.imported_patterns),
                                            ("skip_projects", "Skip projects", cfg.skip_projects)):
                field_input = c.addStringValueInput(input_id, label, "; ".join(values))
                field_input.tooltip = hint

            for evt, handler in ((cmd.inputChanged, InputChangedHandler()),
                                 (cmd.validateInputs, ValidateHandler()),
                                 (cmd.execute, ExecuteHandler()),
                                 (cmd.destroy, DestroyHandler())):
                evt.add(handler)
                _handlers.append(handler)
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                f"FusionArchiver dialog failed:\n{traceback.format_exc()}")


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        if args.input.id != "browse":
            return
        args.input.value = False
        ui = adsk.core.Application.get().userInterface
        dlg = ui.createFolderDialog()
        dlg.title = "FusionArchiver: choose output folder"
        if dlg.showDialog() == adsk.core.DialogResults.DialogOK:
            args.inputs.itemById("output_folder").value = dlg.folder


class ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            _read_dialog(args.inputs).validate(require_output=True)
            args.areInputsValid = True
        except core.ConfigError:
            args.areInputsValid = False


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        cfg = _read_dialog(args.command.commandInputs)
        try:
            cfg.save(SETTINGS_PATH)
        except OSError:
            pass  # settings are a convenience; the export can still run
        _pending["cfg"] = cfg
        _pending["started"] = True
        # Documents cannot be opened while a command is executing; start after it closes.
        adsk.core.Application.get().fireCustomEvent(START_EVENT_ID, "")


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending["started"]:
            _cleanup(terminate=True)


class StartHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            _archive(_pending["cfg"])
        finally:
            _cleanup(terminate=True)


# --------------------------------------------------------------------------
# Export run
# --------------------------------------------------------------------------
def _archive(cfg):
    app = adsk.core.Application.get()
    ui = app.userInterface
    out_root = cfg.output_folder
    log_fh = None
    pd = None
    try:
        os.makedirs(out_root, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_fh = open(os.path.join(out_root, f"log_{stamp}.txt"), "a", encoding="utf-8")

        def log(msg):
            log_fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
            log_fh.flush()

        log(f"FusionArchiver {core.VERSION} settings: {cfg.to_dict()}")

        pd = ui.createProgressDialog()
        pd.isCancelButtonShown = True
        pd.cancelButtonText = "Stop"
        pd.show(f"FusionArchiver {core.VERSION}", "Starting...", 0, 1, 0)
        adsk.doEvents()

        def tick(msg):
            pd.message = msg
            adsk.doEvents()
            return not pd.wasCancelled

        sized = {"done": False}

        def progress(i, n, msg):
            if not sized["done"]:
                pd.maximumValue = max(n, 1)
                sized["done"] = True
            pd.progressValue = i
            pd.message = f"Exporting {i + 1} of {n}\n{msg}"
            adsk.doEvents()
            return not pd.wasCancelled

        summary = core.run(FusionSource(app, log, tick), out_root, cfg, log=log, progress=progress)
        pd.hide()
        pd = None
        ui.messageBox(
            f"FusionArchiver {core.VERSION}\n\n"
            f"Documents: {summary.total}\nExported OK: {summary.exported}\n"
            f"Already done (skipped): {summary.skipped_done}\n"
            f"Non-Fusion files downloaded: {summary.originals}\n"
            f"Non-Fusion files skipped: {summary.skipped_non_design}\n"
            f"Deferred: {summary.deferred}\n"
            f"Partial/failed: {summary.partial}\nOpen failed: {summary.failed_open}\n"
            f"By category: {summary.by_category}\n"
            f"{'STOPPED early. Run again to resume.' if summary.cancelled else ''}\n\n"
            f"See inventory.csv in {out_root}")
    except Exception:
        msg = traceback.format_exc()
        if log_fh:
            log_fh.write(msg)
        ui.messageBox(f"FusionArchiver failed:\n{msg}")
    finally:
        if pd is not None:
            pd.hide()
        if log_fh:
            log_fh.close()


def _cleanup(terminate=False):
    app = adsk.core.Application.get()
    with contextlib.suppress(Exception):  # may not be registered yet
        app.unregisterCustomEvent(START_EVENT_ID)
    with contextlib.suppress(Exception):  # Fusion may already have removed it
        cmd_def = app.userInterface.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
    _handlers.clear()
    _pending.update(cfg=None, started=False)
    if terminate:
        adsk.terminate()


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        existing = ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()
        with contextlib.suppress(Exception):  # left over from a crashed run
            app.unregisterCustomEvent(START_EVENT_ID)
        start_event = app.registerCustomEvent(START_EVENT_ID)
        start_handler = StartHandler()
        start_event.add(start_handler)
        _handlers.append(start_handler)

        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, "FusionArchiver", "Bulk-export the latest version of every document")
        created = CreatedHandler()
        cmd_def.commandCreated.add(created)
        _handlers.append(created)
        cmd_def.execute()
        adsk.autoTerminate(False)  # keep the script alive until the dialog/run finishes
    except Exception:
        ui.messageBox(f"FusionArchiver failed to start:\n{traceback.format_exc()}")


def stop(context):
    with contextlib.suppress(Exception):  # never raise while Fusion is stopping the script
        _cleanup(terminate=False)

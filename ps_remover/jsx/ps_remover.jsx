//@target photoshop
/*
 * ps_remover.jsx - the Photoshop side of ps-remover.
 *
 * The Python package replaces the PSR_CONFIG placeholder below with a JSON
 * job description and runs the script through Photoshop's scripting bridge
 * (COM on Windows, AppleScript "do javascript" on macOS). The value of the
 * script is a JSON string that describes the outcome.
 *
 * Without a config (File > Scripts > Browse...) it removes the current
 * selection of the active document with content-aware fill.
 *
 * ExtendScript implements ECMAScript 3: there is no JSON object, no
 * Array.prototype.forEach/map/indexOf, no String.prototype.trim, and reserved
 * words cannot be used as property names.
 */

var PSR_CONFIG = /*PSR_CONFIG*/null;

var PSR_DEFAULTS = {
    action: "remove_current",   // "remove" | "open" | "remove_current"
    input: null,                // photo to open ("remove", "open")
    output: null,               // file to save the result to
    ops: [],                    // selection shapes, see psrSelectShape()
    subject: false,             // keep only what Select > Subject finds inside the shapes
    expectedSize: null,         // [width, height] the shapes were drawn against
    method: "content-aware",    // "content-aware" | "transparent"
    expand: 4,                  // grow the selection by this many pixels first
    feather: 0,                 // feather radius in pixels
    keepOpen: true,             // show the result in Photoshop afterwards
    jpegQuality: 12,
    report: "alert"             // "alert" shows a dialog, "return" only returns JSON
};

var PSR_HISTORY_NAME = "선택 영역 제거 (ps-remover)";
var PSR_AREA_CHANNEL = "ps-remover area";
var PSR_SAVE_FORMATS = { jpg: "jpeg", jpeg: "jpeg", jpe: "jpeg", png: "png", tif: "tiff", tiff: "tiff", psd: "psd" };

function cTID(s) { return charIDToTypeID(s); }
function sTID(s) { return stringIDToTypeID(s); }

// ------------------------------------------------------------------ actions

function psrRun(rawConfig) {
    var startedAt = new Date().getTime();
    var cfg = psrWithDefaults(rawConfig);
    var result = { ok: false, action: cfg.action, warnings: [] };
    var job = { doc: null };
    var settings = null;
    try {
        result.photoshopVersion = String(app.version);
        settings = psrPushSettings();
        if (cfg.action == "remove") {
            psrActionRemove(cfg, result, job);
        } else if (cfg.action == "open") {
            psrActionOpen(cfg, result);
        } else if (cfg.action == "remove_current") {
            psrActionRemoveCurrent(cfg, result);
        } else {
            throw psrError("알 수 없는 작업입니다: " + cfg.action);
        }
        result.ok = true;
    } catch (e) {
        result.ok = false;
        result.error = psrDescribeError(e);
        psrAfterError(cfg, job, result);
    }
    if (settings) psrPopSettings(settings);
    result.elapsedMs = new Date().getTime() - startedAt;
    if (cfg.report == "alert") psrAlertResult(result);
    return psrToJson(result);
}

// Open the photo, select the shapes, remove them and save a copy.
function psrActionRemove(cfg, result, job) {
    var file = psrInputFile(cfg);
    if (!cfg.output) throw psrError("결과를 저장할 경로가 지정되지 않았습니다.");

    var countBefore = app.documents.length;
    var doc = app.open(file);
    if (app.documents.length == countBefore) {
        // Photoshop handed back a document the user already had open. Work on
        // a duplicate so that document and its unsaved changes stay untouched.
        doc = doc.duplicate(psrStripExtension(psrFileName(file)) + " (ps-remover)");
        result.warnings.push("사진이 이미 Photoshop에 열려 있어서 복사본으로 작업했습니다.");
    }
    job.doc = doc;
    app.activeDocument = doc;
    result.document = psrDocumentInfo(doc);

    var scale = psrShapeScale(doc, cfg, result);
    psrPrepareDocument(doc, cfg, result);
    psrAtPixelResolution(doc, function () {
        psrBuildSelection(doc, psrScaleOps(cfg.ops, scale), cfg.subject, result);
        psrRefineSelection(doc, cfg);
        result.selectionBounds = psrSelectionBounds(doc);
        psrRemoveSelected(doc, cfg.method);
    });

    var saved = psrSave(doc, cfg.output, cfg, result);
    result.output = saved.fsName;

    // The working document is still linked to the original file; close it so
    // a later Ctrl+S can never overwrite the original, and show the saved copy.
    doc.close(SaveOptions.DONOTSAVECHANGES);
    job.doc = null;
    if (cfg.keepOpen) {
        try {
            psrShowFile(saved, result);
        } catch (e) {
            // The result is saved; failing to display it is not a failure of the job.
            result.warnings.push("결과는 저장했지만 Photoshop에서 열지 못했습니다: " + psrDescribeError(e));
        }
        psrBringToFront();
    }
}

function psrActionOpen(cfg, result) {
    var doc = app.open(psrInputFile(cfg));
    app.activeDocument = doc;
    result.document = psrDocumentInfo(doc);
    psrBringToFront();
}

// Remove whatever the user selected in Photoshop, as one undoable step.
function psrActionRemoveCurrent(cfg, result) {
    if (app.documents.length === 0) throw psrError("Photoshop에 열려 있는 문서가 없습니다.");
    var doc = app.activeDocument;
    result.document = psrDocumentInfo(doc);
    if (!psrHasSelection(doc)) {
        throw psrError("Photoshop에 선택 영역이 없습니다. 선택 도구로 지울 부분을 먼저 선택하세요.");
    }
    psrCheckEditable(doc, cfg.method);
    psrInHistory(doc, PSR_HISTORY_NAME, function () {
        psrAtPixelResolution(doc, function () {
            psrRefineSelection(doc, cfg);
            result.selectionBounds = psrSelectionBounds(doc);
            psrRemoveSelected(doc, cfg.method);
        });
    });
    if (cfg.output) {
        result.output = psrSave(doc, cfg.output, cfg, result).fsName;
    }
    psrBringToFront();
}

function psrAfterError(cfg, job, result) {
    if (!job.doc) return;
    try {
        if (cfg.keepOpen) {
            // Leave the photo (with its selection) open so the user can finish by hand.
            app.activeDocument = job.doc;
            result.leftOpen = true;
        } else {
            job.doc.close(SaveOptions.DONOTSAVECHANGES);
        }
    } catch (ignore) {}
    job.doc = null;
}

// ---------------------------------------------------------------- document

// Make the document something content-aware fill can work on: a supported
// color mode and bit depth, with the visible image on one pixel layer.
function psrPrepareDocument(doc, cfg, result) {
    if (doc.mode == DocumentMode.BITMAP) {
        doc.changeMode(ChangeMode.GRAYSCALE);
    } else if (doc.mode == DocumentMode.INDEXEDCOLOR || doc.mode == DocumentMode.DUOTONE) {
        doc.changeMode(ChangeMode.RGB);
    }
    if (doc.bitsPerChannel == BitsPerChannelType.THIRTYTWO) {
        doc.bitsPerChannel = BitsPerChannelType.SIXTEEN;
    }

    if (doc.layers.length > 1) {
        try {
            doc.mergeVisibleLayers();
        } catch (e) {
            doc.flatten();
        }
        result.warnings.push("여러 레이어를 하나로 합친 뒤 작업했습니다. (원본 파일은 바뀌지 않습니다)");
    } else {
        doc.activeLayer = doc.layers[0];
    }
    var layer = doc.activeLayer;
    if (!psrIsPixelLayer(layer) && layer.typename == "ArtLayer") {
        try { layer.rasterize(RasterizeType.ENTIRELAYER); } catch (ignore) {}
    }
    if (!psrIsPixelLayer(doc.activeLayer)) {
        doc.flatten();
    }
    layer = doc.activeLayer;
    if (!psrIsPixelLayer(layer)) {
        throw psrError("이 문서의 레이어 구성은 처리할 수 없습니다. 이미지를 병합한 뒤 다시 시도하세요.");
    }
    if (!layer.isBackgroundLayer) {
        if (layer.allLocked) layer.allLocked = false;
        if (layer.pixelsLocked) layer.pixelsLocked = false;
        if (cfg.method == "transparent" && layer.transparentPixelsLocked) layer.transparentPixelsLocked = false;
    }
}

function psrCheckEditable(doc, method) {
    var layer = doc.activeLayer;
    if (!psrIsPixelLayer(layer)) {
        throw psrError("선택한 레이어 '" + layer.name + "'는 일반 픽셀 레이어가 아닙니다. " +
            "픽셀 레이어를 선택하거나 레이어를 래스터화한 뒤 다시 시도하세요.");
    }
    if (!layer.isBackgroundLayer) {
        if (layer.allLocked || layer.pixelsLocked) {
            throw psrError("선택한 레이어가 잠겨 있습니다. 잠금을 해제한 뒤 다시 시도하세요.");
        }
        if (method == "transparent" && layer.transparentPixelsLocked) {
            throw psrError("레이어의 투명 픽셀이 잠겨 있어 투명하게 지울 수 없습니다.");
        }
    }
    if (doc.bitsPerChannel == BitsPerChannelType.THIRTYTWO) {
        throw psrError("32비트 문서는 지원하지 않습니다. 16비트나 8비트로 바꾼 뒤 다시 시도하세요.");
    }
    var mode = doc.mode;
    if (mode == DocumentMode.BITMAP || mode == DocumentMode.INDEXEDCOLOR ||
            mode == DocumentMode.DUOTONE || mode == DocumentMode.MULTICHANNEL) {
        throw psrError("이 색상 모드에서는 제거할 수 없습니다. 이미지 > 모드에서 RGB로 바꾼 뒤 다시 시도하세요.");
    }
}

function psrIsPixelLayer(layer) {
    return layer && layer.typename == "ArtLayer" && layer.kind == LayerKind.NORMAL;
}

// Photoshop measures some selection distances in points. At 72 ppi a point is
// exactly one pixel, so do the work there. Only the resolution metadata
// changes; no pixels are resampled.
function psrAtPixelResolution(doc, fn) {
    var resolution = doc.resolution;
    var changed = Math.abs(resolution - 72) > 0.001;
    if (changed) doc.resizeImage(undefined, undefined, 72, ResampleMethod.NONE);
    try {
        fn();
    } finally {
        if (changed) doc.resizeImage(undefined, undefined, resolution, ResampleMethod.NONE);
    }
}

// Run fn as a single history state so the user can undo it in one step.
var PSR_PENDING = null;

function psrRunPending() {
    try {
        PSR_PENDING.fn();
    } catch (e) {
        PSR_PENDING.error = e;
    }
}

function psrInHistory(doc, name, fn) {
    var pending = { fn: fn, error: null };
    PSR_PENDING = pending;
    try {
        doc.suspendHistory(name, "psrRunPending()");
    } finally {
        PSR_PENDING = null;
    }
    if (pending.error) throw pending.error;
}

// ---------------------------------------------------------------- selection

// If the photo Photoshop opened is not the size the shapes were drawn on, scale
// them as long as the aspect ratio matches.
function psrShapeScale(doc, cfg, result) {
    var width = psrPx(doc.width);
    var height = psrPx(doc.height);
    var expected = cfg.expectedSize;
    if (!expected || (Math.abs(width - expected[0]) < 0.5 && Math.abs(height - expected[1]) < 0.5)) {
        return [1, 1];
    }
    var ratio = (width / height) / (expected[0] / expected[1]);
    if (Math.abs(ratio - 1) > 0.01) {
        var rotated = Math.abs(width - expected[1]) < 0.5 && Math.abs(height - expected[0]) < 0.5;
        throw psrError("Photoshop에서 연 사진 크기(" + width + "x" + height + ")가 영역을 선택한 사진 크기(" +
            expected[0] + "x" + expected[1] + ")와 다릅니다." +
            (rotated ? " 사진의 회전(EXIF 방향) 정보가 다르게 적용된 것 같습니다." : ""));
    }
    result.warnings.push("Photoshop에서 연 사진 크기(" + width + "x" + height + ")에 맞게 선택 영역 좌표를 조정했습니다.");
    return [width / expected[0], height / expected[1]];
}

function psrScaleOps(ops, scale) {
    var sx = scale[0], sy = scale[1];
    if (sx == 1 && sy == 1) return ops;
    var scaled = [];
    for (var i = 0; i < ops.length; i++) {
        var op = ops[i];
        var copy = { op: op.op, mode: op.mode };
        if (op.box) copy.box = [op.box[0] * sx, op.box[1] * sy, op.box[2] * sx, op.box[3] * sy];
        if (op.points) {
            copy.points = [];
            for (var j = 0; j < op.points.length; j++) {
                copy.points.push([op.points[j][0] * sx, op.points[j][1] * sy]);
            }
        }
        scaled.push(copy);
    }
    return scaled;
}

function psrBuildSelection(doc, ops, useSubject, result) {
    doc.selection.deselect();
    var started = false;
    for (var i = 0; i < ops.length; i++) {
        var subtract = ops[i].mode == "subtract";
        if (subtract && !started) continue; // nothing to subtract from yet
        psrSelectShape(ops[i], subtract ? "SbtF" : (started ? "AddT" : "setd"));
        started = true;
    }
    if (useSubject) psrApplySubject(doc, started, result);
    if (!psrHasSelection(doc)) {
        throw psrError("선택 영역이 비어 있습니다. 지울 부분을 다시 선택하세요.");
    }
}

// op: { op: "rect" | "ellipse", box: [left, top, right, bottom] }
//  or { op: "polygon", points: [[x, y], ...] }
// eventId: "setd" (new selection), "AddT" (add) or "SbtF" (subtract).
function psrSelectShape(op, eventId) {
    var desc = new ActionDescriptor();
    var ref = new ActionReference();
    ref.putProperty(cTID("Chnl"), cTID("fsel"));
    desc.putReference(cTID("null"), ref);
    if (op.op == "polygon") {
        var polygon = new ActionDescriptor();
        var points = new ActionList();
        for (var i = 0; i < op.points.length; i++) {
            var point = new ActionDescriptor();
            point.putUnitDouble(cTID("Hrzn"), cTID("#Pxl"), op.points[i][0]);
            point.putUnitDouble(cTID("Vrtc"), cTID("#Pxl"), op.points[i][1]);
            points.putObject(cTID("Pnt "), point);
        }
        polygon.putList(cTID("Pts "), points);
        desc.putObject(cTID("T   "), cTID("Plgn"), polygon);
        desc.putBoolean(cTID("AntA"), true);
    } else if (op.op == "rect" || op.op == "ellipse") {
        var box = new ActionDescriptor();
        box.putUnitDouble(cTID("Top "), cTID("#Pxl"), op.box[1]);
        box.putUnitDouble(cTID("Left"), cTID("#Pxl"), op.box[0]);
        box.putUnitDouble(cTID("Btom"), cTID("#Pxl"), op.box[3]);
        box.putUnitDouble(cTID("Rght"), cTID("#Pxl"), op.box[2]);
        if (op.op == "ellipse") {
            desc.putObject(cTID("T   "), cTID("Elps"), box);
            desc.putBoolean(cTID("AntA"), true);
        } else {
            desc.putObject(cTID("T   "), cTID("Rctn"), box);
        }
    } else {
        throw psrError("알 수 없는 선택 도형입니다: " + op.op);
    }
    executeAction(cTID(eventId), desc, DialogModes.NO);
}

// Narrow the drawn area down to the object Select > Subject finds inside it.
function psrApplySubject(doc, hasDrawnArea, result) {
    if (!hasDrawnArea) {
        try {
            psrSelectSubject();
        } catch (e) {
            throw psrError("'피사체 선택'을 실행하지 못했습니다 (Photoshop CC 2018 이상 필요): " + psrDescribeError(e));
        }
        return;
    }
    var area = psrStoreSelection(doc, PSR_AREA_CHANNEL);
    try {
        var found = false;
        try {
            psrSelectSubject();
            found = psrHasSelection(doc);
        } catch (ignore) {}
        if (found) {
            doc.selection.load(area, SelectionType.INTERSECT);
            found = psrHasSelection(doc);
        }
        if (!found) {
            doc.selection.load(area, SelectionType.REPLACE);
            result.warnings.push("그린 영역 안에서 피사체를 찾지 못해 그린 영역 전체를 지웠습니다.");
        }
    } finally {
        area.remove();
    }
}

function psrSelectSubject() {
    var desc = new ActionDescriptor();
    desc.putBoolean(sTID("sampleAllLayers"), false);
    executeAction(sTID("autoCutout"), desc, DialogModes.NO);
}

// Select > Save Selection into a new alpha channel (keeps the active channels).
function psrStoreSelection(doc, name) {
    var desc = new ActionDescriptor();
    var ref = new ActionReference();
    ref.putProperty(cTID("Chnl"), cTID("fsel"));
    desc.putReference(cTID("null"), ref);
    desc.putString(cTID("Nm  "), name);
    executeAction(cTID("Dplc"), desc, DialogModes.NO);
    return doc.channels.getByName(name);
}

function psrRefineSelection(doc, cfg) {
    var expand = Math.round(psrClamp(cfg.expand, 0, 100));
    if (expand > 0) doc.selection.expand(new UnitValue(expand, "px"));
    var feather = psrClamp(cfg.feather, 0, 250);
    if (feather >= 0.1) doc.selection.feather(new UnitValue(feather, "px"));
}

function psrHasSelection(doc) {
    try {
        var b = doc.selection.bounds;
        return psrPx(b[2]) > psrPx(b[0]) && psrPx(b[3]) > psrPx(b[1]);
    } catch (e) {
        return false; // Photoshop throws when nothing is selected
    }
}

function psrSelectionBounds(doc) {
    var b = doc.selection.bounds;
    return [psrPx(b[0]), psrPx(b[1]), psrPx(b[2]), psrPx(b[3])];
}

// ------------------------------------------------------------------ removal

function psrRemoveSelected(doc, method) {
    if (method == "transparent") {
        var layer = doc.activeLayer;
        if (layer.isBackgroundLayer) layer.isBackgroundLayer = false;
        doc.selection.clear();
    } else if (method == "content-aware") {
        psrContentAwareFill();
    } else {
        throw psrError("알 수 없는 제거 방식입니다: " + method);
    }
}

// Edit > Fill... > Content-Aware with color adaptation.
function psrContentAwareFill() {
    var desc = new ActionDescriptor();
    desc.putEnumerated(cTID("Usng"), cTID("FlCn"), sTID("contentAware"));
    desc.putBoolean(sTID("contentAwareColorAdaptationFill"), true);
    desc.putUnitDouble(cTID("Opct"), cTID("#Prc"), 100);
    desc.putEnumerated(cTID("Md  "), cTID("BlnM"), cTID("Nrml"));
    executeAction(cTID("Fl  "), desc, DialogModes.NO);
}

// ------------------------------------------------------------------- saving

function psrSave(doc, path, cfg, result) {
    var file = new File(path);
    var format = PSR_SAVE_FORMATS[psrExtension(psrFileName(file))];
    if (!format) {
        throw psrError("지원하지 않는 저장 형식입니다: " + psrFileName(file) + " (jpg, png, tif, psd 중 하나를 쓰세요)");
    }
    if (file.parent && !file.parent.exists) file.parent.create();

    var mode = doc.mode;
    var needsRgb = (format == "png" && mode != DocumentMode.RGB && mode != DocumentMode.GRAYSCALE) ||
        (format == "jpeg" && mode != DocumentMode.RGB && mode != DocumentMode.GRAYSCALE && mode != DocumentMode.CMYK);
    var needs8Bit = format == "jpeg" && doc.bitsPerChannel != BitsPerChannelType.EIGHT;
    var target = doc;
    var converted = false;
    if (needsRgb || needs8Bit) {
        // Convert a throwaway copy instead of the working document.
        target = doc.duplicate(psrStripExtension(psrFileName(file)));
        converted = true;
        if (needsRgb) target.changeMode(ChangeMode.RGB);
        if (needs8Bit) target.bitsPerChannel = BitsPerChannelType.EIGHT;
    }
    try {
        target.saveAs(file, psrSaveOptions(format, cfg), true);
    } finally {
        if (converted) {
            target.close(SaveOptions.DONOTSAVECHANGES);
            app.activeDocument = doc;
        }
    }
    if (!file.exists) throw psrError("저장한 파일을 찾을 수 없습니다: " + file.fsName);
    if (format == "jpeg" && cfg.method == "transparent") {
        result.warnings.push("JPG는 투명도를 저장할 수 없어서 지운 부분이 흰색으로 저장되었습니다. PNG로 저장하면 투명하게 남습니다.");
    }
    return file;
}

function psrSaveOptions(format, cfg) {
    var options;
    if (format == "jpeg") {
        options = new JPEGSaveOptions();
        options.quality = Math.round(psrClamp(cfg.jpegQuality, 0, 12));
        options.embedColorProfile = true;
        options.formatOptions = FormatOptions.STANDARDBASELINE;
        options.matte = MatteType.WHITE;
    } else if (format == "png") {
        options = new PNGSaveOptions();
        options.interlaced = false;
    } else if (format == "tiff") {
        options = new TiffSaveOptions();
        options.imageCompression = TIFFEncoding.TIFFLZW;
        options.embedColorProfile = true;
        options.layers = true;
        options.transparency = true;
    } else {
        options = new PhotoshopSaveOptions();
        options.embedColorProfile = true;
        options.layers = true;
        options.alphaChannels = true;
    }
    return options;
}

// Open the saved result. If an older copy of it is already open, replace it
// unless it has unsaved changes.
function psrShowFile(file, result) {
    var existing = psrFindOpenDocument(file);
    if (existing) {
        if (!existing.saved) {
            app.activeDocument = existing;
            result.warnings.push("같은 이름의 결과 파일이 저장하지 않은 상태로 Photoshop에 열려 있어서 새로 열지 않았습니다.");
            return;
        }
        existing.close(SaveOptions.DONOTSAVECHANGES);
    }
    app.activeDocument = app.open(file);
    result.openedOutput = true;
}

function psrFindOpenDocument(file) {
    for (var i = 0; i < app.documents.length; i++) {
        var doc = app.documents[i];
        try {
            if (psrSamePath(doc.fullName.fsName, file.fsName)) return doc;
        } catch (e) {
            // never saved: no fullName
        }
    }
    return null;
}

function psrSamePath(a, b) {
    // Default Windows and macOS file systems ignore case.
    return a.toLowerCase() == b.toLowerCase();
}

// ------------------------------------------------------------------ helpers

function psrWithDefaults(raw) {
    var cfg = {};
    var key;
    for (key in PSR_DEFAULTS) {
        if (PSR_DEFAULTS.hasOwnProperty(key)) cfg[key] = PSR_DEFAULTS[key];
    }
    if (raw) {
        for (key in raw) {
            if (raw.hasOwnProperty(key) && raw[key] !== null && raw[key] !== undefined) cfg[key] = raw[key];
        }
    }
    return cfg;
}

function psrPushSettings() {
    var saved = {
        rulerUnits: app.preferences.rulerUnits,
        displayDialogs: app.displayDialogs
    };
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;
    return saved;
}

function psrPopSettings(saved) {
    try {
        app.preferences.rulerUnits = saved.rulerUnits;
        app.displayDialogs = saved.displayDialogs;
    } catch (ignore) {}
}

function psrInputFile(cfg) {
    if (!cfg.input) throw psrError("열 사진이 지정되지 않았습니다.");
    var file = new File(cfg.input);
    if (!file.exists) throw psrError("사진 파일을 찾을 수 없습니다: " + cfg.input);
    return file;
}

function psrDocumentInfo(doc) {
    return {
        name: String(doc.name),
        width: psrPx(doc.width),
        height: psrPx(doc.height),
        resolution: Number(doc.resolution),
        layers: doc.layers.length
    };
}

function psrBringToFront() {
    try { app.bringToFront(); } catch (ignore) {}
}

function psrPx(value) {
    return typeof value == "number" ? value : value.as("px");
}

function psrClamp(value, low, high) {
    var n = Number(value);
    if (isNaN(n)) return low;
    return Math.min(high, Math.max(low, n));
}

function psrFileName(file) {
    var path = file.fsName;
    return path.substring(Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\")) + 1);
}

function psrExtension(name) {
    var dot = name.lastIndexOf(".");
    return dot < 0 ? "" : name.substring(dot + 1).toLowerCase();
}

function psrStripExtension(name) {
    var dot = name.lastIndexOf(".");
    return dot <= 0 ? name : name.substring(0, dot);
}

function psrError(message) {
    var error = new Error(message);
    error.psrFriendly = true;
    return error;
}

function psrDescribeError(e) {
    if (e && e.psrFriendly) return e.message;
    var text = (e && e.message) ? String(e.message) : String(e);
    if (e && e.line) text += " (line " + e.line + ")";
    return text;
}

function psrAlertResult(result) {
    var message;
    if (result.ok) {
        message = "선택 영역을 제거했습니다.";
        if (result.output) message += "\n저장 위치: " + result.output;
    } else {
        message = "작업을 끝내지 못했습니다.\n" + result.error;
        if (result.leftOpen) message += "\n\n작업하던 사진은 Photoshop에 열어 두었습니다.";
    }
    for (var i = 0; i < result.warnings.length; i++) message += "\n- " + result.warnings[i];
    alert(message, "ps-remover");
}

// JSON.stringify replacement. Non-ASCII characters are escaped so the result
// survives any code page on its way back through COM or AppleScript.
function psrToJson(value) {
    if (value === null || value === undefined) return "null";
    var type = typeof value;
    if (type == "number") return isFinite(value) ? String(value) : "null";
    if (type == "boolean") return value ? "true" : "false";
    if (type == "string") return psrQuote(value);
    var parts = [];
    if (value instanceof Array) {
        for (var i = 0; i < value.length; i++) parts.push(psrToJson(value[i]));
        return "[" + parts.join(",") + "]";
    }
    for (var key in value) {
        if (value.hasOwnProperty(key) && typeof value[key] != "function") {
            parts.push(psrQuote(key) + ":" + psrToJson(value[key]));
        }
    }
    return "{" + parts.join(",") + "}";
}

function psrQuote(text) {
    text = String(text);
    var out = "\"";
    for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        var code = text.charCodeAt(i);
        if (ch == "\"" || ch == "\\") {
            out += "\\" + ch;
        } else if (code < 0x20 || code > 0x7e) {
            var hex = code.toString(16);
            while (hex.length < 4) hex = "0" + hex;
            out += "\\u" + hex;
        } else {
            out += ch;
        }
    }
    return out + "\"";
}

psrRun(PSR_CONFIG);

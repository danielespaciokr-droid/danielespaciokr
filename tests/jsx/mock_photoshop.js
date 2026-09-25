#!/usr/bin/env node
// Runs a generated ps-remover script against a small fake Photoshop, so the
// script's logic can be tested without Photoshop.
//
//   node mock_photoshop.js SCRIPT.jsx [SCENARIO.json]
//
// Prints {"result", "error", "es3Error", "log", "alerts", "files", "documents"} as JSON.
// The fake only models what ps_remover.jsx uses. ES5+ built-ins are removed
// from the script's global scope, since ExtendScript is ECMAScript 3. When the
// "acorn" package is installed (npm install in this folder) the script is also
// parsed as ECMAScript 3.
'use strict';

const fs = require('fs');
const vm = require('vm');

const [scriptPath, scenarioPath] = process.argv.slice(2);
const script = fs.readFileSync(scriptPath, 'utf8');
const scenario = scenarioPath ? JSON.parse(fs.readFileSync(scenarioPath, 'utf8')) : {};

const log = [];
const alerts = [];
const files = new Set(scenario.files || []);
const savedImages = {};
let context = null;

function record(...entry) {
  log.push(entry);
}

function psError(text) {
  return new Error('General Photoshop error occurred. This functionality may not be available in this version of Photoshop.\n- ' + text);
}

// ------------------------------------------------------------------ enums

function enumOf(name, members) {
  const e = {};
  for (const m of members) e[m] = `${name}.${m}`;
  return e;
}

const DialogModes = enumOf('DialogModes', ['ALL', 'ERROR', 'NO']);
const Units = enumOf('Units', ['PIXELS', 'CM', 'INCHES', 'MM', 'PERCENT', 'POINTS', 'PICAS']);
const SaveOptions = enumOf('SaveOptions', ['DONOTSAVECHANGES', 'PROMPTTOSAVECHANGES', 'SAVECHANGES']);
const DocumentMode = enumOf('DocumentMode', ['RGB', 'CMYK', 'GRAYSCALE', 'LAB', 'BITMAP', 'INDEXEDCOLOR', 'DUOTONE', 'MULTICHANNEL']);
const ChangeMode = enumOf('ChangeMode', ['RGB', 'CMYK', 'GRAYSCALE', 'LAB', 'BITMAP', 'INDEXEDCOLOR', 'MULTICHANNEL']);
const BitsPerChannelType = enumOf('BitsPerChannelType', ['ONE', 'EIGHT', 'SIXTEEN', 'THIRTYTWO']);
const ResampleMethod = enumOf('ResampleMethod', ['NONE', 'BICUBIC', 'BILINEAR', 'NEARESTNEIGHBOR', 'AUTOMATIC']);
const LayerKind = enumOf('LayerKind', ['NORMAL', 'SMARTOBJECT', 'TEXT', 'SOLIDFILL', 'CURVES']);
const SelectionType = enumOf('SelectionType', ['REPLACE', 'EXTEND', 'DIMINISH', 'INTERSECT']);
const FormatOptions = enumOf('FormatOptions', ['STANDARDBASELINE', 'OPTIMIZEDBASELINE', 'PROGRESSIVE']);
const MatteType = enumOf('MatteType', ['NONE', 'WHITE', 'BLACK', 'BACKGROUND', 'FOREGROUND', 'SEMIGRAY', 'NETSCAPE']);
const TIFFEncoding = enumOf('TIFFEncoding', ['NONE', 'TIFFLZW', 'JPEG', 'TIFFZIP']);
const RasterizeType = enumOf('RasterizeType', ['ENTIRELAYER', 'SHAPE', 'TEXTCONTENTS']);

// ---------------------------------------------------------- units & files

class UnitValue {
  constructor(value, unit) {
    this.value = Number(value);
    this.type = unit || 'px';
  }
  as(unit) {
    if (unit !== 'px' || this.type !== 'px') throw new Error(`mock: cannot convert ${this.type} to ${unit}`);
    return this.value;
  }
}

function basename(p) {
  return p.split(/[\\/]/).pop();
}

function dirname(p) {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i > 0 ? p.slice(0, i) : p;
}

class File {
  constructor(path) {
    this.fsName = String(path);
  }
  get exists() {
    return files.has(this.fsName);
  }
  get name() {
    return encodeURI(basename(this.fsName)); // ExtendScript URI-encodes File.name
  }
  get parent() {
    return new Folder(dirname(this.fsName));
  }
}

class Folder {
  constructor(path) {
    this.fsName = String(path);
  }
  get exists() {
    return !(scenario.missingFolders || []).includes(this.fsName);
  }
  create() {
    record('createFolder', this.fsName);
    return true;
  }
}

class JPEGSaveOptions { constructor() { this.kind = 'jpeg'; } }
class PNGSaveOptions { constructor() { this.kind = 'png'; } }
class TiffSaveOptions { constructor() { this.kind = 'tiff'; } }
class PhotoshopSaveOptions { constructor() { this.kind = 'psd'; } }

// ------------------------------------------------------- action manager

function charIDToTypeID(id) {
  if (typeof id !== 'string' || id.length !== 4) throw new Error(`mock: bad charID ${JSON.stringify(id)}`);
  return id;
}

function stringIDToTypeID(id) {
  return id;
}

class ActionDescriptor {
  constructor() { this.data = {}; }
  putReference(key, ref) { this.data[key] = { ref: ref.parts }; }
  putString(key, value) { this.data[key] = String(value); }
  putBoolean(key, value) { this.data[key] = !!value; }
  putInteger(key, value) { this.data[key] = value; }
  putDouble(key, value) { this.data[key] = value; }
  putUnitDouble(key, unit, value) { this.data[key] = { unit, value }; }
  putEnumerated(key, type, value) { this.data[key] = { enumType: type, value }; }
  putObject(key, cls, desc) { this.data[key] = { cls, desc: desc.data }; }
  putList(key, list) { this.data[key] = list.items; }
}

class ActionReference {
  constructor() { this.parts = []; }
  putProperty(cls, prop) { this.parts.push({ cls, property: prop }); }
  putEnumerated(cls, type, value) { this.parts.push({ cls, enumType: type, value }); }
  putName(cls, name) { this.parts.push({ cls, name }); }
  putIndex(cls, index) { this.parts.push({ cls, index }); }
}

class ActionList {
  constructor() { this.items = []; }
  putObject(cls, desc) { this.items.push({ cls, desc: desc.data }); }
  putUnitDouble(unit, value) { this.items.push({ unit, value }); }
}

function shapeBounds(target) {
  const d = target.desc;
  if (target.cls === 'Plgn') {
    const xs = d['Pts '].map(p => p.desc.Hrzn.value);
    const ys = d['Pts '].map(p => p.desc.Vrtc.value);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  return [d.Left.value, d['Top '].value, d.Rght.value, d.Btom.value];
}

function executeAction(event, desc, mode) {
  const data = desc ? desc.data : null;
  record('executeAction', event, data, mode);
  if ((scenario.failEvents || []).includes(event)) throw psError(`The command "${event}" is not currently available.`);
  const doc = app.activeDocument;
  const sel = doc.selection;
  switch (event) {
    case 'setd':
    case 'AddT':
    case 'SbtF': {
      const box = doc.clip(shapeBounds(data['T   ']));
      if (event === 'setd') sel.box = box;
      else if (event === 'AddT') sel.box = union(sel.box, box);
      else if (sel.box && box && contains(box, sel.box)) sel.box = null;
      break;
    }
    case 'autoCutout':
      if (scenario.subjectThrows) throw psError('Select Subject failed.');
      sel.box = scenario.subject ? doc.clip(scenario.subject) : null;
      break;
    case 'Dplc':
      doc.channels.add(data['Nm  '], sel.box);
      break;
    default:
      break;
  }
  return new ActionDescriptor();
}

function union(a, b) {
  if (!a) return b;
  if (!b) return a;
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])];
}

function intersect(a, b) {
  if (!a || !b) return null;
  const r = [Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.min(a[2], b[2]), Math.min(a[3], b[3])];
  return r[2] > r[0] && r[3] > r[1] ? r : null;
}

function contains(outer, inner) {
  return outer[0] <= inner[0] && outer[1] <= inner[1] && outer[2] >= inner[2] && outer[3] >= inner[3];
}

// --------------------------------------------------------------- DOM

class Selection {
  constructor(doc) {
    this.doc = doc;
    this.box = null;
  }
  get bounds() {
    if (!this.box) throw psError('There is no selection.');
    return this.box.map(v => new UnitValue(v, 'px'));
  }
  deselect() {
    record('deselect', this.doc.name);
    this.box = null;
  }
  expand(by) {
    record('expand', by.value, by.type, this.doc._resolution);
    const v = by.value;
    if (this.box) this.box = this.doc.clip([this.box[0] - v, this.box[1] - v, this.box[2] + v, this.box[3] + v]);
  }
  feather(by) {
    record('feather', by.value, by.type, this.doc._resolution);
  }
  clear() {
    const layer = this.doc.activeLayer;
    record('clear', layer.name, layer.isBackgroundLayer);
    if (layer.isBackgroundLayer) throw new Error('mock: clearing the background layer paints the background color');
  }
  load(channel, type) {
    record('loadSelection', channel.name, type);
    if (type === SelectionType.REPLACE) this.box = channel.box;
    else if (type === SelectionType.INTERSECT) this.box = intersect(this.box, channel.box);
    else throw new Error('mock: unsupported selection type ' + type);
  }
}

class Channel {
  constructor(owner, name, box) {
    this.owner = owner;
    this.name = name;
    this.box = box;
  }
  remove() {
    record('removeChannel', this.name);
    this.owner.items = this.owner.items.filter(c => c !== this);
  }
}

class Channels {
  constructor() { this.items = []; }
  add(name, box) {
    const channel = new Channel(this, name, box);
    this.items.push(channel);
    return channel;
  }
  getByName(name) {
    const channel = this.items.find(c => c.name === name);
    if (!channel) throw new Error('No such element');
    return channel;
  }
  get length() { return this.items.length; }
}

class Layer {
  constructor(spec) {
    this.name = spec.name || 'Layer 1';
    this.typename = spec.typename || 'ArtLayer';
    this.kind = LayerKind[spec.kind || 'NORMAL'];
    this._background = !!spec.background;
    this._locks = {
      allLocked: !!spec.allLocked,
      pixelsLocked: !!spec.pixelsLocked,
      transparentPixelsLocked: !!spec.transparentPixelsLocked,
    };
  }
  get isBackgroundLayer() { return this._background; }
  set isBackgroundLayer(value) {
    record('isBackgroundLayer', this.name, value);
    this._background = !!value;
    if (!value) this.name = 'Layer 0';
  }
  get allLocked() { return this._locks.allLocked; }
  set allLocked(v) { record('allLocked', this.name, v); this._locks.allLocked = v; }
  get pixelsLocked() { return this._locks.pixelsLocked; }
  set pixelsLocked(v) { record('pixelsLocked', this.name, v); this._locks.pixelsLocked = v; }
  get transparentPixelsLocked() { return this._locks.transparentPixelsLocked; }
  set transparentPixelsLocked(v) { record('transparentPixelsLocked', this.name, v); this._locks.transparentPixelsLocked = v; }
  rasterize(type) {
    record('rasterize', this.name, type);
    if (this.typename !== 'ArtLayer') throw new Error('mock: cannot rasterize ' + this.typename);
    this.kind = LayerKind.NORMAL;
  }
}

class Document {
  constructor(spec) {
    this.name = spec.name;
    this._file = spec.file || null;
    this._width = spec.width;
    this._height = spec.height;
    this._resolution = spec.resolution || 72;
    this._mode = DocumentMode[spec.mode || 'RGB'];
    this._bits = BitsPerChannelType[spec.bits || 'EIGHT'];
    this.layers = (spec.layers || [{ name: 'Background', background: true }]).map(l => new Layer(l));
    this.activeLayer = this.layers[0];
    this.saved = spec.saved !== false;
    this.selection = new Selection(this);
    this.channels = new Channels();
    this.closed = false;
    if (spec.selection) this.selection.box = spec.selection;
  }
  clip(box) {
    return intersect(box, [0, 0, this._width, this._height]);
  }
  get fullName() {
    if (!this._file) throw new Error('The document has not yet been saved.');
    return new File(this._file);
  }
  get width() { return new UnitValue(this._width, 'px'); }
  get height() { return new UnitValue(this._height, 'px'); }
  get resolution() { return this._resolution; }
  get mode() { return this._mode; }
  get bitsPerChannel() { return this._bits; }
  set bitsPerChannel(value) {
    record('bitsPerChannel', this.name, value);
    this._bits = value;
  }
  changeMode(mode) {
    record('changeMode', this.name, mode);
    this._mode = 'DocumentMode.' + mode.split('.')[1];
  }
  resizeImage(width, height, resolution, method) {
    record('resizeImage', this.name, width === undefined ? null : width, height === undefined ? null : height, resolution, method);
    if (width !== undefined || height !== undefined || method !== ResampleMethod.NONE) {
      throw new Error('mock: only resolution changes without resampling are expected');
    }
    this._resolution = resolution;
  }
  mergeVisibleLayers() {
    record('mergeVisibleLayers', this.name);
    if (scenario.mergeFails) throw psError('The command "Merge Visible" is not currently available.');
    const merged = new Layer({ name: 'Merged' });
    this.layers = [merged];
    this.activeLayer = merged;
  }
  flatten() {
    record('flatten', this.name);
    const background = new Layer({ name: 'Background', background: true });
    this.layers = [background];
    this.activeLayer = background;
  }
  duplicate(name) {
    record('duplicate', this.name, name === undefined ? null : name);
    const copy = new Document({
      name: name || this.name + ' copy',
      width: this._width,
      height: this._height,
      resolution: this._resolution,
    });
    copy._mode = this._mode;
    copy._bits = this._bits;
    copy.selection.box = this.selection.box;
    app._add(copy);
    return copy;
  }
  saveAs(file, options, asCopy) {
    const opts = Object.assign({}, options);
    record('saveAs', this.name, file.fsName, opts, asCopy, this._resolution, this._bits, this._mode);
    if (scenario.saveFails) throw psError('Could not save because of a disk error.');
    files.add(file.fsName);
    savedImages[file.fsName] = { width: this._width, height: this._height, resolution: this._resolution };
  }
  close(option) {
    record('close', this.name, option);
    this.closed = true;
    app._remove(this);
  }
  suspendHistory(name, code) {
    record('suspendHistory', this.name, name, code);
    vm.runInContext(code, context);
  }
}

const app = {
  documents: [],
  _active: null,
  preferences: { rulerUnits: Units.CM },
  displayDialogs: DialogModes.ALL,
  version: scenario.version || '26.0.0',
  get activeDocument() {
    if (!this._active) throw new Error('No such element');
    return this._active;
  },
  set activeDocument(doc) {
    if (!doc || doc.closed) throw new Error('mock: activating a closed document');
    this._active = doc;
  },
  open(file) {
    record('open', file.fsName);
    if (!files.has(file.fsName)) throw psError('File not found.');
    const existing = this.documents.find(d => d._file === file.fsName);
    if (existing) {
      this._active = existing;
      return existing;
    }
    const spec = Object.assign({ width: 100, height: 100 }, savedImages[file.fsName],
      (scenario.images || {})[file.fsName], { file: file.fsName, name: basename(file.fsName) });
    const doc = new Document(spec);
    this._add(doc);
    return doc;
  },
  bringToFront() {
    record('bringToFront');
  },
  _add(doc) {
    this.documents.push(doc);
    this._active = doc;
  },
  _remove(doc) {
    this.documents.splice(this.documents.indexOf(doc), 1);
    if (this._active === doc) this._active = this.documents[this.documents.length - 1] || null;
  },
};

for (const spec of scenario.openDocuments || []) {
  app._add(new Document(Object.assign({ width: 100, height: 100 }, spec, { name: spec.name || basename(spec.file || 'Untitled-1') })));
}

// ----------------------------------------------------------------- run

const sandbox = {
  app, File, Folder, UnitValue,
  ActionDescriptor, ActionReference, ActionList,
  charIDToTypeID, stringIDToTypeID, executeAction,
  DialogModes, Units, SaveOptions, DocumentMode, ChangeMode, BitsPerChannelType, ResampleMethod,
  LayerKind, SelectionType, FormatOptions, MatteType, TIFFEncoding, RasterizeType,
  JPEGSaveOptions, PNGSaveOptions, TiffSaveOptions, PhotoshopSaveOptions,
  $: { os: scenario.os || 'Windows 10' },
  alert(message, title) {
    alerts.push([String(message), title === undefined ? null : String(title)]);
  },
};

// Remove what ExtendScript (ECMAScript 3) does not have.
const ES3_PRELUDE = `
(function (global) {
  var removed = {
    'Array.prototype': ['forEach', 'map', 'filter', 'some', 'every', 'reduce', 'reduceRight', 'indexOf',
      'lastIndexOf', 'find', 'findIndex', 'includes', 'fill', 'keys', 'values', 'entries', 'flat', 'flatMap'],
    'String.prototype': ['trim', 'trimStart', 'trimEnd', 'startsWith', 'endsWith', 'includes', 'repeat',
      'padStart', 'padEnd'],
    'Array': ['isArray', 'from', 'of'],
    'Object': ['keys', 'create', 'assign', 'freeze', 'values', 'entries', 'getPrototypeOf'],
    'Function.prototype': ['bind'],
    'Date': ['now']
  };
  var owners = { 'Array.prototype': Array.prototype, 'String.prototype': String.prototype, 'Array': Array,
    'Object': Object, 'Function.prototype': Function.prototype, 'Date': Date };
  for (var owner in removed) {
    for (var i = 0; i < removed[owner].length; i++) delete owners[owner][removed[owner][i]];
  }
  delete global.JSON;
})(this);
`;

let es3Error = null;
try {
  const acorn = require('acorn');
  try {
    acorn.parse(script, { ecmaVersion: 3, allowReserved: false });
  } catch (e) {
    es3Error = e.message;
  }
} catch (e) {
  es3Error = undefined; // acorn not installed: not checked
}

context = vm.createContext(sandbox);
vm.runInContext(ES3_PRELUDE, context);
let result = null;
let error = null;
try {
  result = vm.runInContext(script, context, { filename: 'ps_remover.jsx' });
} catch (e) {
  error = String((e && e.stack) || e);
}

process.stdout.write(JSON.stringify({
  result: result === undefined ? null : result,
  error,
  es3Error: es3Error === undefined ? 'not checked' : es3Error,
  log,
  alerts,
  files: Array.from(files).sort(),
  preferences: { rulerUnits: app.preferences.rulerUnits, displayDialogs: app.displayDialogs },
  documents: app.documents.map(d => ({
    name: d.name,
    file: d._file,
    resolution: d._resolution,
    selection: d.selection.box,
    channels: d.channels.items.map(c => c.name),
    active: d === app._active,
  })),
}));

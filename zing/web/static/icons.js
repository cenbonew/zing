/* zing web UI — canonical inline-SVG icon set.
 *
 * Plain browser global, no modules, no external deps. Exposes:
 *   window.ZING_ICONS         name -> raw <svg> markup string
 *   window.zingIcon(name, o)  -> <svg> string with class/size applied
 *
 * Every icon is a minimal line-icon: viewBox 0 0 24 24, stroke=currentColor so
 * it inherits the surrounding text color, 1.5–2px stroke, rounded caps/joins,
 * no fills, no hardcoded colors. Designed to read cleanly at ~16–22px.
 *
 * Usage (inline into innerHTML):
 *   el.innerHTML = zingIcon("bell");                 // 1em, class "zicon"
 *   el.innerHTML = zingIcon("check", {size: 18, cls: "ok"});
 *   el.innerHTML = ZING_ICONS.bolt;                  // raw markup, no sizing
 *
 * CSS note: the SVG carries class "zicon" (plus any opts.cls). Color it via the
 * parent's `color` (or `.zicon{color:var(--teal)}`); size via font-size or the
 * `size` option. Strokes use currentColor, so a single `color` change restyles
 * both the glyph and any adjacent text. Recommended baseline:
 *   .zicon{vertical-align:middle;flex:none}
 */
(function () {
  "use strict";

  // Inner markup only (paths/shapes); the <svg> wrapper is assembled in zingIcon
  // so width/height/class can be injected consistently. currentColor everywhere.
  var PATHS = {
    // bolt — lightning logo / run energy
    bolt: '<path d="M13 2L4.5 13.5H11l-1 8.5L19.5 10H13l0-8z"/>',
    // bell — monitoring / alerts
    bell:
      '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>' +
      '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    // chart — history / trends
    chart:
      '<path d="M4 19V5"/><path d="M4 19h16"/>' +
      '<path d="M7 15l4-5 3 3 5-7"/>',
    // toolbox — tools surface
    toolbox:
      '<rect x="3" y="8" width="18" height="12" rx="2"/>' +
      '<path d="M9 8V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/>' +
      '<path d="M3 13h18"/><path d="M10 13v2"/><path d="M14 13v2"/>',
    // lock — local-key reassurance
    lock:
      '<rect x="4" y="10" width="16" height="11" rx="2"/>' +
      '<path d="M8 10V7a4 4 0 0 1 8 0v3"/>' +
      '<path d="M12 14v3"/>',
    // folder — history grouping
    folder:
      '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    // warning — caution
    warning:
      '<path d="M12 3.5L21.5 20H2.5L12 3.5z"/>' +
      '<path d="M12 10v4"/><path d="M12 17.5v.5"/>',
    // check — pass
    check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
    // x — fail / close
    x: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    // arrowRight — CTA arrow
    arrowRight: '<path d="M4 12h15"/><path d="M13 6l6 6-6 6"/>',
    // arrowLeft — back
    arrowLeft: '<path d="M20 12H5"/><path d="M11 6l-6 6 6 6"/>',
    // chevronDown — disclosure caret
    chevronDown: '<path d="M6 9l6 6 6-6"/>',
    // cornerDownRight — nested line
    cornerDownRight: '<path d="M6 4v8a2 2 0 0 0 2 2h11"/><path d="M15 10l4 4-4 4"/>',
    // play — run now
    play: '<path d="M7 5l12 7-12 7z"/>',
    // info — informational finding
    info:
      '<circle cx="12" cy="12" r="9"/>' +
      '<path d="M12 11v5"/><path d="M12 7.5v.5"/>',
    // search — search
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    // trash — delete
    trash:
      '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>' +
      '<path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>' +
      '<path d="M10 11v6"/><path d="M14 11v6"/>',
    // refresh — re-run
    refresh:
      '<path d="M20 11a8 8 0 1 0-1.5 5.5"/>' +
      '<path d="M20 5v6h-6"/>',
    // download — export
    download:
      '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/>' +
      '<path d="M4 20h16"/>',
    // plus — add
    plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  };

  var ICONS = {};
  var SVG_OPEN_TPL =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" ' +
    'fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round" ';

  function buildSvg(inner, width, height, cls) {
    return (
      SVG_OPEN_TPL +
      'width="' + width + '" height="' + height + '" class="' + cls + '" ' +
      'aria-hidden="true" focusable="false">' +
      inner +
      "</svg>"
    );
  }

  // Pre-render a default 1em version of each icon as ZING_ICONS[name].
  Object.keys(PATHS).forEach(function (name) {
    ICONS[name] = buildSvg(PATHS[name], "1em", "1em", "zicon");
  });

  function zingIcon(name, opts) {
    var inner = PATHS[name];
    if (inner == null) return ""; // unknown name → empty, never throw
    opts = opts || {};
    var size = opts.size == null ? "1em" : opts.size;
    if (typeof size === "number") size = String(size);
    var cls = "zicon" + (opts.cls ? " " + opts.cls : "");
    return buildSvg(inner, size, size, cls);
  }

  window.ZING_ICONS = ICONS;
  window.zingIcon = zingIcon;
})();

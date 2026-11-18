// Project builder: SLRUnreal extract payload -> SLR Translator project object.
//
// Consumes the extract command's table JSON (backend contract v2):
//   {version, command:"extract", game_dir, graph, text_table: [{japanese,
//    english, context, field, file, path_id, entry_index}], stats}
// (text_table fields mirror SLRUnity's strings.csv columns — Phase 3 Task 6
// verified the mirroring; `entry_index` is the Phase-4 write-back key.)
//
// buildProject groups rows by `file` (the payload asset path — zen package
// name for IoStore games, .uexp name for pak games) and emits SLR's
// files[fileKey].data rows [[japanese, ""], ...] in table order. The
// per-file row -> text_table position map travels in
// options.slrunreal.rowMap so export can fill english back EXACTLY where
// extraction found it (no index arithmetic at export time).
//
// Pure-lib rule: only node builtins here (path); no NW.js / SLR globals.
"use strict";

const path = require("path");

// common.containJapanese's exact character class (SLR www/js/common.js:974).
const JP_CHAR = /[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf]/;

function buildProject(table, meta) {
  const rows = table.text_table || [];
  const files = {};
  const rowMap = {};                     // fileKey -> [text_table positions]
  const contexts = {};                   // fileKey -> [[context], ...]
  // Phase 7 collect-all-visible (SLRUnity v2.2 parity): the scan yields
  // strings WITHOUT Japanese too (official English UI labels, glyphs).
  // When SLR's "Japanese source" general option is on, those rows carry
  // SLR's own "cell without Japanese" tag (gray — the same mark batch's
  // No-Jap check paints and batch-blacklists), so the Japanese rows stay
  // the obvious batch target. Off -> every row untagged.
  //
  // ZenName rows (zen name-batch strings — DataTable row keys, internal
  // object names) carry the backend's risk tag instead: yellow (usually
  // safe, check first) or red (structural identifier — never by
  // default). Both are skipped by SLR's batch translation; the user
  // promotes rows by retagging. Gray never competes — ZenName rows are
  // collected only when they contain Japanese.
  const tagNoJp = meta.japaneseSource === true;
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const fileKey = row.file;
    if (!fileKey) continue;
    if (!files[fileKey]) {
      files[fileKey] = { data: [], tags: [] };
      rowMap[fileKey] = [];
      contexts[fileKey] = [];
    }
    files[fileKey].data.push([row.japanese, ""]);
    const risk = row.tag === "yellow" || row.tag === "red" ? row.tag : "";
    files[fileKey].tags.push(risk ? [risk]
      : tagNoJp && !JP_CHAR.test(row.japanese || "") ? ["gray"] : []);
    rowMap[fileKey].push(i);
    contexts[fileKey].push([row.context || path.basename(fileKey)]);
  }
  for (const fileKey of Object.keys(files)) {
    const base = path.basename(fileKey);
    files[fileKey] = {
      path: fileKey, dirname: path.dirname(fileKey), basename: base,
      extension: path.extname(base).slice(1),
      data: files[fileKey].data,
      context: contexts[fileKey],
      tags: files[fileKey].tags, parameters: [],
    };
  }
  return {
    gameEngine: "unreal", parser: "unreal", gameTitle: meta.gameTitle,
    loc: meta.gameDir, projectId: meta.cache.cacheID, cache: meta.cache,
    files,
    options: { slrunreal: {
      contract: 2,
      gameDir: meta.gameDir,
      // Phase 8a: the staging tree (captured at create, packed into the
      // .slrproj by Save Project) — the portable patch export builds
      // purely from it; gameDir only serves the full-rebuild mode.
      stageDir: meta.stageDir || "",
      // self-contained project: the FULL extract JSON is staged under
      // <cachePath>/data/ (e2e-fix7 precedent) and export re-reads it;
      // rowMap fills english back at the exact extracted positions.
      rowMap,
      stats: table.stats || {},
    } },
  };
}

// export-side helper: fill `english` into a staged extract table from the
// project's files (rows [jp, translation]) + options.slrunreal.rowMap.
// Blank/whitespace translations stay "" (= not patched; the backend's
// contract). Returns the number of filled rows.
function applyTranslations(table, files, rowMap) {
  let filled = 0;
  for (const fileKey of Object.keys(rowMap)) {
    const file = files[fileKey];
    if (!file) continue;
    const positions = rowMap[fileKey];
    (file.data || []).forEach((row, i) => {
      const en = row && typeof row[1] === "string" ? row[1].trim() : "";
      if (en && positions[i] != null) {
        const trow = table.text_table[positions[i]];
        if (trow && trow.japanese === row[0]) {     // sanity: same source
          trow.english = row[1];
          filled++;
        }
      }
    });
  }
  return filled;
}

module.exports = { buildProject, applyTranslations, JP_CHAR };

const test = require("node:test");
const assert = require("node:assert");
const { buildProject, applyTranslations, JP_CHAR } = require("../lib/projectBuilder");

const TABLE = {
  version: 2, command: "extract", game_dir: "E:/games/SomeUE",
  graph: { order: ["a.uasset"], files: {} },
  text_table: [
    { japanese: "テスト１", english: "", context: "a.uasset/0/<frame>/0", field: "<frame>/0",
      file: "sample_a/Content/DT_a.uasset", path_id: 0, entry_index: 5 },
    { japanese: "テスト２", english: "", context: "a.uasset/1/<frame>/1", field: "<frame>/1",
      file: "sample_a/Content/DT_a.uasset", path_id: 1, entry_index: 6 },
    { japanese: "こちら", english: "", context: "b.uexp/0/<frame>/0", field: "<frame>/0",
      file: "SampleGameB/Content/DT_b.uexp", path_id: 0, entry_index: 9 },
  ],
  stats: { text_entries: 3 },
};

function meta() {
  return { gameDir: "E:/games/SomeUE", gameTitle: "SomeUE",
           cache: { cacheID: "id1", cachePath: "cache" } };
}

test("buildProject groups rows by file in table order", () => {
  const p = buildProject(TABLE, meta());
  assert.equal(p.gameEngine, "unreal");
  const f = p.files["sample_a/Content/DT_a.uasset"];
  assert.deepEqual(f.data, [["テスト１", ""], ["テスト２", ""]]);
  assert.equal(p.files["SampleGameB/Content/DT_b.uexp"].data.length, 1);
  assert.deepEqual(p.options.slrunreal.rowMap["sample_a/Content/DT_a.uasset"], [0, 1]);
  assert.deepEqual(p.options.slrunreal.rowMap["SampleGameB/Content/DT_b.uexp"], [2]);
});

test("applyTranslations fills english at the mapped positions only", () => {
  const p = buildProject(TABLE, meta());
  const table = JSON.parse(JSON.stringify(TABLE));
  p.files["sample_a/Content/DT_a.uasset"].data[0][1] = "test one";
  p.files["SampleGameB/Content/DT_b.uexp"].data[0][1] = "  this one  ";
  const n = applyTranslations(table, p.files, p.options.slrunreal.rowMap);
  assert.equal(n, 2);
  assert.equal(table.text_table[0].english, "test one");
  assert.equal(table.text_table[1].english, "");           // untouched
  assert.equal(table.text_table[2].english, "  this one  "); // trimmed at fill, verbatim stored
});

test("applyTranslations refuses row/source mismatches", () => {
  const p = buildProject(TABLE, meta());
  const table = JSON.parse(JSON.stringify(TABLE));
  table.text_table[0].japanese = "すり替えられた";
  p.files["sample_a/Content/DT_a.uasset"].data[0][1] = "test one";
  const n = applyTranslations(table, p.files, p.options.slrunreal.rowMap);
  assert.equal(n, 0);                                       // sanity gate: no blind fill
});

test("JP_CHAR matches SLR's class", () => {
  assert.equal(JP_CHAR.test("テスト"), true);
  assert.equal(JP_CHAR.test("hello"), false);
});

// Phase 7 collect-all-visible: rows without Japanese carry SLR's gray
// "cell without Japanese" tag when the Japanese-source option is on
// (SLRUnity v2.2 parity); off -> untagged.
const VISIBLE_TABLE = {
  version: 2, command: "extract", game_dir: "E:/games/SomeUE",
  graph: { order: ["a.uexp"], files: {} },
  text_table: [
    { japanese: "New Game", english: "", context: "c", field: "f",
      file: "WG_Title.uexp", path_id: 0, entry_index: 0 },
    { japanese: "はじめる", english: "", context: "c", field: "f",
      file: "WG_Title.uexp", path_id: 1, entry_index: 1 },
    { japanese: "☆☆☆☆☆", english: "", context: "c", field: "f",
      file: "WG_Title.uexp", path_id: 2, entry_index: 2 },
  ],
  stats: { text_entries: 3 },
};

test("gray no-Japanese tags follow the japaneseSource option", () => {
  const on = buildProject(VISIBLE_TABLE, { ...meta(), japaneseSource: true });
  assert.deepEqual(on.files["WG_Title.uexp"].tags,
    [["gray"], [], ["gray"]]);
  const off = buildProject(VISIBLE_TABLE, meta());
  assert.deepEqual(off.files["WG_Title.uexp"].tags, [[], [], []]);
});

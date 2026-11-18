const test = require("node:test");
const assert = require("node:assert");
const { siblingDir, toForwardSlashes, fileStem } = require("../lib/paths");

test("fileStem", () => {
  assert.equal(fileStem("a.uasset"), "a");
  assert.equal(fileStem("noext"), "noext");
  assert.equal(fileStem(".hidden"), ".hidden");      // dotfile: stem keeps it
  assert.equal(fileStem("a.b.c"), "a.b");
});

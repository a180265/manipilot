// P04：合法零值（hoverZ=0）不被前端真假值兜底吞掉。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "../scripts/motor_debug_page.html"), "utf8");

test("hoverZ inputs use null-fallback instead of truthy default", () => {
  // 旧实现 parseFloat(...) || 2 会把合法 0 替换成 2（真假值误判）。
  assert.doesNotMatch(html, /parseFloat\(\$\("#hoverZ"\)\.value\) \|\| \d/);
  const uses = html.match(/NUM_FALLBACK\(\$\("#hoverZ"\)\.value\)/g) || [];
  assert.ok(uses.length >= 3, `expected >=3 NUM_FALLBACK uses, got ${uses.length}`);
});

test("NUM_FALLBACK keeps zero and rejects garbage as null", () => {
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const vm = require("node:vm");
  const context = vm.createContext({Number});
  const fn = vm.runInContext(
    `(${script.match(/function NUM_FALLBACK[\s\S]*?\n}/)[0]})`, context);
  assert.equal(fn("0"), 0);
  assert.equal(fn("0.0"), 0);
  assert.equal(fn("2.5"), 2.5);
  assert.equal(fn(""), null);
  assert.equal(fn("abc"), null);
});

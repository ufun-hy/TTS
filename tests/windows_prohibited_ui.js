// Validate Windows Studio's prohibited speech UI and inline syntax without a browser.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync("web/text-studio.html", "utf8");
for (const script of ["/text-studio-prohibited-rules.js", "/text-studio-prohibited.js"]) {
  assert(html.includes('<script src="' + script + '"></script>'), script + " is not loaded");
}
const inline = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
assert(inline.length >= 1, "no inline workbench scripts");
for (const [i, match] of inline.entries()) {
  new vm.Script(match[1], { filename: "studio-inline-" + i + ".js" });
}
const analyzer = require("../web/text-studio-prohibited.js");
const original = "免费试吃。口感清甜。";
const first = analyzer.analyze(original);
assert.strictEqual(first.text, "口感清甜。");
assert.strictEqual(first.blocked.length, 1);
const hit = first.blocked[0];
const review = {candidate_index:0,candidate_text:original,start:hit.start,text:hit.text,labels:hit.labels,
    rules_version:analyzer.rulesVersion,confirmed_at:"2026-10-20T13:00:00Z"};
assert.strictEqual(analyzer.analyze(original, [review]).text, original);
assert.strictEqual(analyzer.analyze(original.replace("清甜","清香"),[review]).blocked.length,1);
assert(html.includes("prohibited_reviews:p.prohibited_reviews||[]"), "review not saved");
assert(html.includes("prohibited_reviews:Array.isArray(item.prohibited_reviews)"), "review not restored");
assert(html.includes("prohibited_reviews:p.prohibited_reviews||[]})"), "review not sent to preview");
console.log("Windows prohibited speech UI and script syntax: PASS");

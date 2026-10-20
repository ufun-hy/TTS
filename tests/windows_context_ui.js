const assert=require("assert"),fs=require("fs"),vm=require("vm");
const html=fs.readFileSync("web/text-studio.html","utf8");
assert(html.includes('id="contextCopyBtn"'));
assert(html.includes("async function generalizeContextAll()"));
assert(html.includes("group.selected_variant_id=variant.id"));
assert(html.includes("context_groups:state.contextGroups"));
for(const [i,m] of [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].entries()){
  new vm.Script(m[1],{filename:"studio-"+i+".js"});
}
console.log("Windows context UI syntax and integration hooks: PASS");

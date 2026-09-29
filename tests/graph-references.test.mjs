import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/graphReferences.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } });
const { resolveGraphReferences } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const nodes = [
  { id: "a::node:1", label: "Beam", packId: "a", properties: { original_id: "node:1" } },
  { id: "b::node:1", label: "Beam", packId: "b", properties: { original_id: "node:1" } },
  { id: "a::node:2", label: "Beam", packId: "a", properties: { original_id: "node:2" } },
];

test("explicit source references override ambiguous answer labels and stay in the queried pack", () => {
  assert.deepEqual(resolveGraphReferences(nodes, "a", [{ id: "node:1" }], "Beam"), ["a::node:1"]);
});

test("empty or undisplayed explicit references never guess from labels", () => {
  assert.deepEqual(resolveGraphReferences(nodes, "a", [], "Beam"), []);
  assert.deepEqual(resolveGraphReferences(nodes, "a", [{ id: "missing" }], "Beam"), []);
});

test("namespaced IDs without pack metadata still enforce scope", () => {
  const bareNodes = [{ id: "a::node:1" }, { id: "b::node:1" }];
  assert.deepEqual(resolveGraphReferences(bareNodes, "a", [{ id: "node:1" }]), ["a::node:1"]);
});

test("legacy text fallback enforces pack scope and token boundaries", () => {
  assert.deepEqual(resolveGraphReferences(nodes, "a", undefined, "Beam"), ["a::node:1", "a::node:2"]);
  assert.deepEqual(resolveGraphReferences(nodes, "a", undefined, "Beamwork node:10"), []);
});

test("duplicate references do not create duplicate highlights", () => {
  assert.deepEqual(resolveGraphReferences([nodes[0], nodes[0]], "a", [{ id: "node:1" }]), ["a::node:1"]);
});

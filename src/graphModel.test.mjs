// Run with the project's existing TypeScript dependency: node --test src/graphModel.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('./graphModel.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } });
const { indexEdges, neighborhood, visibleNodes, edgePath } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('indexes directed predicates, self-loops and disconnected nodes without dangling edges', () => {
  const graph = indexEdges(new Set(['a', 'b', 'isolated']), [
    { source: 'a', target: { id: 'b' }, relation: 'owns' },
    { source: 'b', target: 'a', relation: 'uses' },
    { source: 'a', target: 'a', relation: 'reflects' },
    { source: 'a', target: 'absent' },
  ]);
  assert.equal(graph.links.length, 3);
  assert.deepEqual(graph.adjacency.get('a').map(({ dir }) => dir), ['→', '←', '↺']);
  assert.equal(graph.degrees.get('a'), 4);
  assert.equal(graph.degrees.get('isolated'), 0);
  assert.deepEqual(graph.links.map(({ lane }) => lane), [-0.5, 0.5, 0]);
});

test('hop focus visits cyclic and parallel edges once', () => {
  const graph = indexEdges(new Set(['a', 'b', 'c', 'd']), [
    { source: 'a', target: 'a' }, { source: 'a', target: 'b' },
    { source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 'c', target: 'a' },
  ]);
  assert.deepEqual([...neighborhood(graph.adjacency, 'b', 1)], ['b', 'a', 'c']);
  assert.deepEqual([...neighborhood(graph.adjacency, 'b', 4)], ['b', 'a', 'c']);
});

test('uniform-degree graph respects node budget while selected/highlighted nodes bypass filters', () => {
  const ranked = Array.from({ length: 5000 }, (_, i) => ({ id: String(i), degree: 2, type: i === 4999 ? 'hidden' : 'shown' }));
  const options = { minDegree: 2, hiddenTypes: new Set(['hidden']), focus: null, forced: new Set(['4999']), limit: 4000 };
  const result = visibleNodes(ranked, options);
  assert.equal(result.nodes.length, 4000);
  assert.equal(result.nodes[0].id, '4999');
  assert.equal(result.capped, true);
  const filtered = visibleNodes(ranked, { ...options, minDegree: 3, focus: new Set(['1']) });
  assert.deepEqual(filtered.nodes.map(({ id }) => id), ['4999']);
  assert.equal(filtered.capped, false);
});

test('changing forced selection removes a previously forced hidden node', () => {
  const ranked = [{ id: 'a', degree: 0, type: 'hidden' }, { id: 'b', degree: 0, type: 'shown' }];
  const options = { minDegree: 0, hiddenTypes: new Set(['hidden']), focus: null, forced: new Set(['a']), limit: 4000 };
  assert.equal(visibleNodes(ranked, options).nodes.length, 2);
  assert.deepEqual(visibleNodes(ranked, { ...options, forced: new Set(['b']) }).nodes.map(({ id }) => id), ['b']);
});

test('search finds names and IDs beyond the display cap while preserving forced nodes', () => {
  const ranked = Array.from({ length: 5000 }, (_, i) => ({ id: `node:${i}`, label: i === 4999 ? 'Hidden Beam' : `Part ${i}`, degree: 0, type: 'part' }));
  const options = { minDegree: 0, hiddenTypes: new Set(), focus: null, forced: new Set(['node:1']), limit: 4000 };
  assert.ok(!visibleNodes(ranked, options).nodes.some(({ id }) => id === 'node:4999'));
  const byName = visibleNodes(ranked, { ...options, search: '  hidden BEAM  ' });
  assert.deepEqual(byName.nodes.map(({ id }) => id), ['node:1', 'node:4999']);
  assert.equal(byName.capped, false);
  assert.deepEqual(visibleNodes(ranked, { ...options, search: 'NODE:4999' }).nodes.map(({ id }) => id), ['node:1', 'node:4999']);
  assert.equal(visibleNodes(ranked, { ...options, search: ' ' }).nodes.length, 4000);
  assert.deepEqual(visibleNodes(ranked, { ...options, search: 'not found' }).nodes.map(({ id }) => id), ['node:1']);
});

test('search combines with degree, type and focus filters', () => {
  const ranked = [
    { id: 'a', label: 'Beam', degree: 2, type: 'shown' },
    { id: 'b', label: 'Beam', degree: 0, type: 'shown' },
    { id: 'c', label: 'Beam', degree: 2, type: 'hidden' },
    { id: 'd', label: 'Beam', degree: 2, type: 'shown' },
  ];
  const result = visibleNodes(ranked, { minDegree: 1, hiddenTypes: new Set(['hidden']), focus: new Set(['a', 'b', 'c']), forced: new Set(), limit: 4000, search: 'beam' });
  assert.deepEqual(result.nodes.map(({ id }) => id), ['a']);
});

test('parallel reverse edges curve on separate sides with arrows clipped to their own targets', () => {
  const a = { x: 0, y: 0, r: 5 }, b = { x: 100, y: 0, r: 10 };
  const forward = edgePath(a, b, { a: 'a', b: 'b', rel: 'owns', lane: -0.5 });
  const reverse = edgePath(b, a, { a: 'b', b: 'a', rel: 'uses', lane: 0.5 });
  assert.ok(forward.control.y < 0 && reverse.control.y > 0);
  assert.ok(Math.abs(Math.hypot(forward.end.x - b.x, forward.end.y - b.y) - b.r) < 1e-8);
  assert.ok(Math.abs(Math.hypot(reverse.end.x - a.x, reverse.end.y - a.y) - a.r) < 1e-8);
  assert.ok(forward.angle > -Math.PI / 2 && forward.angle < Math.PI / 2);
  assert.ok(Math.abs(reverse.angle) > Math.PI / 2);
});

test('self-loop paths fan out and coincident nodes keep finite geometry', () => {
  const node = { x: 0, y: 0, r: 10 };
  const first = edgePath(node, node, { a: 'a', b: 'a', rel: 'self', lane: 0 });
  const second = edgePath(node, node, { a: 'a', b: 'a', rel: 'other', lane: 1 });
  assert.ok(first.control2);
  assert.ok(second.label.y < first.label.y);
  const coincident = edgePath(node, node, { a: 'a', b: 'b', rel: 'other', lane: 0 });
  assert.ok(Object.values(coincident).every((value) => typeof value === 'number' ? Number.isFinite(value) : Object.values(value).every(Number.isFinite)));
});

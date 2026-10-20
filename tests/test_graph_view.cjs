// Run with: node --test tests/test_graph_view.cjs (no npm dependencies).
const test = require('node:test');
const assert = require('node:assert/strict');
const {layout, packComponents, exportBounds, imageSize} = require('../frontend/graph_view.js');

const nodes = Array.from({length: 7}, (_, index) => ({id: index + 1, label: `Protein ${index + 1}`}));
const edges = [
  {from: 1, to: 2}, {from: 2, to: 3}, {from: 3, to: 1}, // Directed cycle.
  {from: 1, to: 2}, {from: 2, to: 1}, // Parallel and reverse assertions.
  {from: 4, to: 5}, {from: 6, to: 6}, // Separate component and self loop.
];

for (const mode of ['hierarchy', 'circle']) {
  test(`${mode} keeps cyclic, disconnected and isolated nodes with deterministic positions`, () => {
    const positions = layout(nodes, edges, mode, 320);
    assert.equal(Object.keys(positions).length, nodes.length);
    assert.equal(new Set(Object.values(positions).map(p => `${p.x},${p.y}`)).size, nodes.length);
    for (const point of Object.values(positions)) assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y));
    assert.deepEqual(positions, layout([...nodes].reverse(), [...edges].reverse(), mode, 320));
    assert.deepEqual(layout([], [], mode, 320), {});
  });
}

test('hierarchy puts a directed chain into successive columns and spacing changes positions', () => {
  const chain = [{from: 1, to: 2}, {from: 2, to: 3}];
  const compact = layout(nodes.slice(0, 3), chain, 'hierarchy', 320);
  const wide = layout(nodes.slice(0, 3), chain, 'hierarchy', 460);
  assert.ok(compact[1].x < compact[2].x && compact[2].x < compact[3].x);
  assert.ok(wide[3].x - wide[1].x > compact[3].x - compact[1].x);
});

test('long relationship labels get room between hierarchy columns', () => {
  const pair = nodes.slice(0, 2);
  const short = layout(pair, [{from: 1, to: 2, label: 'AFFECTS'}], 'hierarchy', 320);
  const long = layout(pair, [{from: 1, to: 2, label: 'CONDITION_WITH_A_LONG_RELATIONSHIP_LABEL'}], 'hierarchy', 320);
  assert.ok(long[2].x - long[1].x > short[2].x - short[1].x);
});

test('packing distant components preserves relative positions within each component', () => {
  const positions = {1: {x: 0, y: 0}, 2: {x: 400, y: 60}, 3: {x: 10000, y: 10000}};
  const boxes = Object.fromEntries(Object.entries(positions).map(([id, p]) =>
    [id, {left: p.x - 50, right: p.x + 50, top: p.y - 20, bottom: p.y + 20}]));
  const packed = packComponents(nodes.slice(0, 3), [{from: 1, to: 2}], positions, boxes, 320);
  assert.equal(packed[2].x - packed[1].x, 400);
  assert.equal(packed[2].y - packed[1].y, 60);
  assert.ok(Math.hypot(packed[3].x - packed[1].x, packed[3].y - packed[1].y) < 2000);
});

test('export bounds retain distant nodes, curved edge labels and self loops outside the viewport', () => {
  const positions = {1: {x: -3000, y: 1000}, 2: {x: 4000, y: -2000}};
  const boxes = [{left: -3120, right: -2880, top: 950, bottom: 1050},
    {left: 3880, right: 4120, top: -2050, bottom: -1950}];
  const simple = exportBounds(boxes, [], positions, () => ({width: 0, height: 0}));
  const curved = exportBounds(boxes, [{from: 1, to: 2, smooth: {roundness: .85}}], positions,
    () => ({width: 800, height: 320}));
  assert.ok(simple.left < boxes[0].left && simple.right > boxes[1].right);
  assert.ok(curved.top < simple.top && curved.bottom > simple.bottom);
  const loop = exportBounds([boxes[0]], [{from: 1, to: 1, selfReference: {size: 600}}], positions,
    () => ({width: 400, height: 200}));
  assert.ok(loop.left < -4200 && loop.bottom > 2200);
});

test('image dimensions include the legend and respect requested resolution without unsafe allocations', () => {
  const bounds = {left: -100, top: -100, right: 900, bottom: 900};
  const normal = imageSize(bounds, 1, 2);
  const high = imageSize(bounds, 2, 2);
  assert.equal(high.width, normal.width * 2);
  assert.equal(high.height, normal.height * 2);
  assert.ok(normal.height > normal.graphHeight + 100);
  assert.throws(() => imageSize({...bounds, right: 1000000}, 3, 2), /降低清晰度/);
  assert.throws(() => imageSize({...bounds, bottom: NaN}, 2, 2), /降低清晰度/);
});

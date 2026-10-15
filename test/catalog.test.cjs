const test = require('node:test');
const assert = require('node:assert/strict');
const catalog = require('../catalog.js');

const filters = { mode: 'nearby', headroomPercent: '20', nearbyTolerancePercent: '20',
    requireRuntime: true, requireOutlets: true, includeUnknown: true };
const load = { watts: 560, va: 700, outlets: 8, runtime: 10 };

test('catalog request keeps current load and independent exploratory filters', () => {
    const request = catalog.buildSearchRequest({ ...filters, query: '  UPS-X  ', maxPriceTHB: '5000' }, load);
    assert.equal(request.query, 'UPS-X');
    assert.equal(request.requiredWatts, 560);
    assert.equal(request.requiredVa, 700);
    assert.equal(request.requiredOutlets, 8);
    assert.equal(request.pageSize, 25);
    assert.equal(request.maxPriceTHB, 5000);
    assert.equal(catalog.buildSearchRequest({ ...filters, mode: 'strict' }, load).includeUnknown, false);
});

test('catalog request rejects invalid margins, count and budget instead of changing them', () => {
    assert.throws(() => catalog.buildSearchRequest({ ...filters, headroomPercent: 51 }, load));
    assert.throws(() => catalog.buildSearchRequest({ ...filters, maxPriceTHB: -1 }, load));
    assert.throws(() => catalog.buildSearchRequest(filters, { ...load, outlets: 1.5 }));
    assert.throws(() => catalog.buildSearchRequest(filters, { ...load, watts: Infinity }));
});

test('manual selections survive result pages, allow removal and cap the optimizer pool', () => {
    const selection = catalog.createSelection();
    selection.toggle(11, 'Model A', true);
    selection.toggle(22, 'Model B', true);
    assert.deepEqual(selection.ids(), [11, 22]);
    selection.toggle(11, 'Model A', false);
    assert.deepEqual(selection.ids(), [22]);
    for (let id = 100; id < 199; id++) selection.toggle(id, 'Model', true);
    assert.throws(() => selection.toggle(200, 'Too many', true));
    assert.equal(selection.ids().length, 100);
    selection.clear();
    assert.deepEqual(selection.ids(), []);
});

test('disabled filters remain visibly disabled and unknown runtime discloses curve bounds', () => {
    const text = catalog.describeCheck('runtime', { status: 'unknown', enabled: false, required: 10, available: null },
        { runtimeLoadRangeWatts: { minimum: 300, maximum: 600 } });
    assert.match(text, /ไม่ได้ใช้กรอง/);
    assert.match(text, /300–600 W/);
    assert.doesNotMatch(text, /: ผ่าน/);
});

test('catalog uses authenticated same-origin paginated search without database credentials', async t => {
    const previous = global.fetch;
    t.after(() => { global.fetch = previous; });
    let sent;
    global.fetch = async (url, options) => {
        sent = { url, ...options };
        return { status: 200, ok: true, json: async () => ({ models: [], total: 0 }) };
    };
    const result = await catalog.search({ mode: 'strict', page: 2, pageSize: 25 });
    assert.equal(sent.url, '/api/catalog/search');
    assert.equal(sent.credentials, 'same-origin');
    assert.equal(JSON.parse(sent.body).page, 2);
    assert.equal(result.total, 0);
    assert.equal(sent.headers.Authorization, undefined);
});

test('catalog reports authentication failure instead of treating it as an empty catalog', async t => {
    const previous = global.fetch;
    t.after(() => { global.fetch = previous; });
    global.fetch = async () => ({ status: 401, ok: false });
    await assert.rejects(catalog.search({}), /เข้าสู่ระบบ/);
});

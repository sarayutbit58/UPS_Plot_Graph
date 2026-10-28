const assert = require('node:assert/strict');
const test = require('node:test');

const { buildRequest } = require('./optimization.js');

test('optimizer calls the hosted same-origin API without a localhost restriction', async t => {
    const previousFetch = global.fetch;
    const previousLocation = global.location;
    t.after(() => { global.fetch = previousFetch; global.location = previousLocation; });
    global.location = { hostname: 'ups.example.test' };
    let sent;
    global.fetch = async (url, options) => {
        sent = { url, ...options };
        return { status: 200, ok: true, json: async () => ({ validDesignCount: 1 }) };
    };
    const result = await require('./optimization.js').optimize({ selectedProductIds: [11] });
    assert.equal(result.validDesignCount, 1);
    assert.equal(sent.url, '/api/optimizations');
    assert.equal(sent.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(sent.body).selectedProductIds, [11]);
});

test('optimizer request keeps a two-plug device whole while applying forecast load', () => {
    const request = buildRequest([
        { name: 'Server', load: 100, unit: 'W', plugs: 2 },
        { name: 'Switch', load: 125, unit: 'VA', plugs: 1 },
    ], 30, 200, 220);

    assert.equal(request.runtimeRequirementMinutes, 30);
    assert.deepEqual(request.devices, [
        { name: 'Server', quantity: 1, watt: 110, va: 137.5, priority: 50,
            plugs: [{ connector: null, count: 2 }] },
        { name: 'Switch', quantity: 1, watt: 110, va: 137.5, priority: 50,
            plugs: [{ connector: null, count: 1 }] },
    ]);
    assert.equal(request.devices.reduce((sum, row) => sum + row.watt * row.quantity, 0), 220);
    assert.equal(request.devices.reduce((sum, row) => sum + row.va * row.quantity, 0), 275);
});

test('optimizer request keeps physical device quantities and plugs per device', () => {
    const request = buildRequest([
        { name: 'Switch', load: 120, unit: 'W', quantity: 3, plugs: 2 },
        { name: 'Router', load: 125, unit: 'VA', quantity: 2, plugs: 1 },
    ], 30, 560, 616);

    assert.deepEqual(request.devices, [
        { name: 'Switch', quantity: 3, watt: 132, va: 165, priority: 50,
            plugs: [{ connector: null, count: 2 }] },
        { name: 'Router', quantity: 2, watt: 110, va: 137.5, priority: 50,
            plugs: [{ connector: null, count: 1 }] },
    ]);
    assert.equal(request.devices.reduce((sum, row) => sum + row.watt * row.quantity, 0), 616);
    assert.equal(request.devices.reduce((sum, row) => sum + row.va * row.quantity, 0), 770);
});

test('optimizer request rejects invalid device quantity and excessive outlet instances', () => {
    for (const quantity of [0, 1.5]) {
        assert.throws(() => buildRequest([
            { name: 'Router', load: 100, unit: 'W', quantity, plugs: 1 },
        ], 30, 100, 100), /จำนวนอุปกรณ์/);
    }
    assert.throws(() => buildRequest([
        { name: 'Router', load: 100, unit: 'W', quantity: 10001, plugs: 1 },
    ], 30, 100, 100), /10,000/);
    assert.throws(() => buildRequest([
        { name: 'Router', load: 100, unit: 'W', quantity: 6000, plugs: 2 },
    ], 30, 600000, 600000), /10,000/);
});

test('optimizer request rejects malformed plug counts instead of defaulting them to one', () => {
    for (const plugs of [0, '0', -1, 1.5, '', ' ', null, true, 'abc', Infinity]) {
        assert.throws(() => buildRequest([
            { name: 'Router', load: 100, unit: 'W', quantity: 1, plugs },
        ], 30, 100, 100), /จำนวนปลั๊ก/);
    }
});

test('optimizer request defaults an omitted plug count to one untyped plug', () => {
    const request = buildRequest([{ name: 'Router', load: 100, unit: 'W' }], 30, 100, 100);
    assert.deepEqual(request.devices[0].plugs, [{ connector: null, count: 1 }]);
});

test('optimizer sends connector groups per physical device without inferring unknown types', () => {
    const request = buildRequest([{
        name: 'Server', load: 250, unit: 'W', quantity: 2, plugs: 3,
        plugGroups: [{ connector: 'IEC_C14', count: 2 }, { connector: null, count: 1 }],
    }], 10, 500, 550);
    assert.deepEqual(request.devices, [{
        name: 'Server', quantity: 2, watt: 275, va: 343.75, priority: 50,
        plugs: [{ connector: 'IEC_C14', count: 2 }, { connector: null, count: 1 }],
    }]);
});

test('optimizer rejects invalid connector groups or counts that do not match the plug total', () => {
    const invalidGroups = [
        [], [{ connector: 'UNIVERSAL', count: 2 }],
        [{ connector: 'IEC_C14', count: 1 }],
        [{ connector: 'IEC_C20', count: 0 }, { connector: null, count: 2 }],
        [{ connector: 'NEMA_5_15P', count: 1.5 }, { connector: null, count: 0.5 }],
        [{ connector: null, count: true }], [{ connector: null, count: '1.00000000000000001' }],
    ];
    for (const plugGroups of invalidGroups) {
        assert.throws(() => buildRequest([{
            name: 'Server', load: 100, unit: 'W', plugs: 2, plugGroups,
        }], 10, 100, 100), /ปลั๊ก|หัวต่อ/);
    }
});

test('optimizer request rejects forecast-only load because it has no source outlet', () => {
    assert.throws(() => buildRequest([], 30, 0, 100), /โหลดอุปกรณ์และจำนวนปลั๊ก/);
});

test('optimizer request preserves outlet-only rows with zero watts', () => {
    const request = buildRequest([
        { name: 'Server', load: 100, unit: 'W', plugs: 1 },
        { name: 'No-load appliance', load: 0, unit: 'W', quantity: 3, plugs: 2 },
    ], 30, 100, 100);

    assert.equal(request.devices.length, 2);
    assert.deepEqual(request.devices[1], {
        name: 'No-load appliance', quantity: 3, watt: 0, va: 0, priority: 50,
        plugs: [{ connector: null, count: 2 }],
    });
});

test('optimizer request ignores blank device rows like the calculator does', () => {
    const request = buildRequest([
        { name: 'Server', load: 100, unit: 'W', plugs: 1 },
        { name: 'Blank row', load: '', unit: 'W', plugs: 4 },
    ], 30, 100, 100);

    assert.equal(request.devices.length, 1);
    assert.equal(request.devices[0].quantity, 1);
});

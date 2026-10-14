const assert = require('node:assert/strict');
const test = require('node:test');

const { buildRequest } = require('./optimization.js');

test('optimizer request preserves outlet count and applies forecast load without adding outlets', () => {
    const request = buildRequest([
        { name: 'Server', load: 100, unit: 'W', plugs: 2 },
        { name: 'Switch', load: 125, unit: 'VA', plugs: 1 },
    ], 30, 200, 220);

    assert.equal(request.runtimeRequirementMinutes, 30);
    assert.deepEqual(request.devices.map(row => row.quantity), [2, 1]);
    assert.equal(request.devices.reduce((sum, row) => sum + row.watt * row.quantity, 0), 220);
    assert.equal(request.devices.reduce((sum, row) => sum + row.va * row.quantity, 0), 275);
});

test('optimizer request rejects forecast-only load because it has no source outlet', () => {
    assert.throws(() => buildRequest([], 30, 0, 100), /โหลดอุปกรณ์และจำนวนปลั๊ก/);
});

test('optimizer request preserves outlet-only rows with zero watts', () => {
    const request = buildRequest([
        { name: 'Server', load: 100, unit: 'W', plugs: 1 },
        { name: 'No-load appliance', load: 0, unit: 'W', plugs: 2 },
    ], 30, 100, 100);

    assert.equal(request.devices.length, 2);
    assert.deepEqual(request.devices[1], {
        name: 'No-load appliance', quantity: 2, watt: 0, va: 0, priority: 50,
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

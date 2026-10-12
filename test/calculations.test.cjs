const test = require('node:test');
const assert = require('node:assert/strict');
const calculations = require('../calculations.js');

test('converts VA with the existing 0.8 PF assumption and applies Forecast to device watts', () => {
    const result = calculations.calculateLoadBreakdown([
        { name: 'Server', load: 120, unit: 'W', plugs: 1 },
        { name: 'Switch', load: 120, unit: 'VA', plugs: 2 },
    ], 10, '%');

    assert.equal(result.deviceLoad, 216);
    assert.equal(result.forecastWatt, 21.6);
    assert.equal(result.totalLoad, 237.6);
    assert.equal(result.totalPlugs, 3);
});

test('adds a fixed-W Forecast and ignores non-finite or non-positive device loads', () => {
    const result = calculations.calculateLoadBreakdown([
        { name: 'Router', load: 100, unit: 'W', plugs: 1 },
        { name: 'Zero', load: 0, unit: 'W', plugs: 1 },
    ], 25, 'W');

    assert.equal(result.deviceLoad, 100);
    assert.equal(result.forecastWatt, 25);
    assert.equal(result.totalLoad, 125);
    assert.equal(result.totalPlugs, 1);
    assert.equal(result.devices.length, 1);
    assert.equal(calculations.calculateLoadBreakdown([{ load: Infinity, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 0.001, unit: 'W', plugs: 1 }], 0, '%').totalLoad, 0.001);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 1e308, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: 1.5 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: Number.MAX_VALUE }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: -1, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: 1 }], -1, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'watts', plugs: 1 }], 10, 'watts').valid, false);
    assert.equal(calculations.calculateLoadBreakdown(null, 0, '%').valid, false);
});

test('calculates required Ah and rejects invalid sizing inputs', () => {
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 85), 11.764706);
    assert.equal(calculations.calculateBatteryAh(0.01, 1, 48, 100), 0.000004);
    assert.equal(calculations.calculateBatteryAh(0, 60, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, -1, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 0), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 49), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 50), 20);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 100), 10);
    assert.equal(calculations.calculateBatteryAh(Infinity, 60, 12, 85), null);
    assert.ok(calculations.calculateBatteryAh(1e308, 1e308, 1e308, 100) > 0);
    assert.ok(calculations.calculateBatteryAh(1e308, 1e-308, 1e-308, 100) > 0);
});

test('calculates runtime with the inverse formula and handles zero or invalid capacity', () => {
    assert.equal(calculations.calculateRuntimeMinutes(11.76, 12, 85, 120), 59.98);
    assert.equal(calculations.calculateRuntimeMinutes(0, 12, 85, 120), 0);
    assert.equal(calculations.calculateRuntimeMinutes(-1, 12, 85, 120), null);
    assert.equal(calculations.calculateRuntimeMinutes(11.76, 12, 85, 0), null);
    assert.equal(calculations.calculateRuntimeMinutes(11.76, 12, 150, 120), null);
    assert.equal(calculations.calculateRuntimeMinutes(11.76, 12, 49, 120), null);
    assert.equal(calculations.calculateRuntimeMinutes(1 / 12, 12, 100, 120), 0.5);
    assert.equal(calculations.calculateRuntimeMinutes(0.25, 12, 100, 120), 1.5);
    assert.ok(Math.abs(calculations.calculateRuntimeMinutes(1 / 6000, 12, 100, 120) - 0.001) < 1e-12);
    assert.equal(calculations.calculateRuntimeMinutes(Number.MIN_VALUE, 12, 100, Number.MAX_VALUE), Number.MIN_VALUE);
    assert.equal(calculations.calculateRuntimeMinutes(1e306, 1e308, 50, 1e308), 3e307);
    const cancellationRuntime = calculations.calculateRuntimeMinutes(1e306, 1e-308, 50, 1e-308);
    assert.ok(Math.abs(cancellationRuntime / 3e307 - 1) < 1e-12);
});

test('builds a unique numeric runtime curve that matches the exact current load', () => {
    const curve = calculations.buildRuntimeCurve(11.76, 12, 85, 100);
    const loads = curve.map(point => point.loadW);
    assert.deepEqual(loads, [50, 100, 150, 200, 250, 300]);
    assert.equal(new Set(loads).size, loads.length);
    assert.equal(curve.find(point => point.currentLoad).runtimeMinutes, 71.97);

    const fractional = calculations.buildRuntimeCurve(11.76, 12, 85, 105.6);
    assert.ok(fractional.some(point => point.loadW === 105.6 && point.currentLoad));
});

test('keeps existing UPS sizing and outlet screening rules at their boundaries', () => {
    assert.equal(calculations.recommendUpsVA(76.92), 100);
    assert.equal(calculations.recommendUpsVA(77), 200);
    assert.equal(calculations.recommendUpsVA(0), 0);
    assert.equal(calculations.recommendUpsVA(1.4e308), null);
    assert.deepEqual(calculations.planOutlets(4), { requiredUpsCount: 1, outletsPerUps: 4 });
    assert.deepEqual(calculations.planOutlets(5), { requiredUpsCount: 1, outletsPerUps: 6 });
    assert.deepEqual(calculations.planOutlets(12), { requiredUpsCount: 1, outletsPerUps: 12 });
    assert.deepEqual(calculations.planOutlets(13), { requiredUpsCount: 2, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(24), { requiredUpsCount: 2, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(25), { requiredUpsCount: 3, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(4.5), { requiredUpsCount: 0, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(-1), { requiredUpsCount: 0, outletsPerUps: 0 });
});

test('escapes text and quoted attribute payloads', () => {
    const payload = '"><img src=x onerror="window.__probe=1">';
    assert.equal(
        calculations.escapeHtml(payload),
        '&quot;&gt;&lt;img src=x onerror=&quot;window.__probe=1&quot;&gt;'
    );
});

test('normalizes valid imports and rejects malformed device/settings data before use', () => {
    const normalized = calculations.normalizeImportData({
        devices: [{ name: 'Server', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: {},
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: 15 },
        forecast: { value: 10, unit: '%' },
    });
    assert.equal(normalized.devices[0].name, 'Server');
    assert.deepEqual(normalized.settings, { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: 15 });
    assert.deepEqual(normalized.forecast, { value: 10, unit: '%' });
    assert.throws(() => calculations.normalizeImportData({ devices: {}, calculatedData: {} }), { name: 'InvalidImportDataError' });
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Bad', load: 1, unit: 'W', plugs: -3 }],
        calculatedData: {},
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [], calculatedData: {}, settings: { backupTime: -2, voltage: 12, efficiency: 85 },
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Server', load: true, unit: 'W', plugs: 1 }], calculatedData: {},
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Server', load: 120, unit: 'W', plugs: 1 }], calculatedData: {},
        settings: { runtimeBatteryAh: 'not a number' },
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Server', load: 120, unit: 'W', plugs: 1 }], calculatedData: {},
        forecast: { value: false, unit: '%' },
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Server', load: 120, unit: 'W', plugs: 1 }], calculatedData: {}, settings: false,
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Server', load: 120, unit: 'W', plugs: 1 }], calculatedData: {},
        settings: { voltage: 13 },
    }));
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Extreme', load: 1e308, unit: 'W', plugs: 1 }], calculatedData: {},
    }), { name: 'InvalidImportDataError' });
});

test('round-trips export settings and Forecast through normalized import', () => {
    const date = new Date('2026-10-12T00:00:00.000Z');
    const exported = calculations.buildExportData({
        devices: [{ name: 'Switch', load: 120, unit: 'VA', watts: 96, plugs: 2 }],
        calculatedData: { totalLoad: 105.6, forecastValue: 10, forecastUnit: '%', forecastWatt: 9.6 },
        settings: { backupTime: 45, voltage: 24, efficiency: 90, runtimeBatteryAh: 30 },
        exportDate: date,
    });
    const imported = calculations.normalizeImportData(exported);
    assert.equal(exported.exportDate, date.toISOString());
    assert.equal(imported.devices[0].unit, 'VA');
    assert.equal(imported.settings.runtimeBatteryAh, 30);
    assert.deepEqual(imported.forecast, { value: 10, unit: '%' });
});

test('keeps compatibility with the previous v1.2 export shape and legacy Forecast watt fields', () => {
    const previousExport = calculations.normalizeImportData({
        version: '1.2',
        devices: [{ name: 'Switch', load: 100, unit: 'W', watts: 100, plugs: 1 }],
        calculatedData: { backupTime: 60, voltage: 12, efficiency: 85, forecastWatt: 15 },
        forecast: { value: 15, unit: 'W', watt: 15 },
        settings: { backupTime: 60, voltage: 12, efficiency: 85 },
    });
    assert.deepEqual(previousExport.forecast, { value: 15, unit: 'W' });
    assert.equal(calculations.calculateLoadBreakdown(previousExport.devices, previousExport.forecast.value, previousExport.forecast.unit).totalLoad, 115);

    const legacyExport = calculations.normalizeImportData({
        devices: [{ name: 'Router', load: 100, unit: 'W' }],
        calculatedData: { forecastWatt: 10 },
        forecastWatt: 10,
    });
    assert.deepEqual(legacyExport.forecast, { value: 10, unit: 'W' });
});

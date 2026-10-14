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

test('keeps displayed load, Forecast, and total aligned at six-decimal precision', () => {
    const roundedComponents = calculations.calculateLoadBreakdown([
        { name: 'Precise', load: 100.0000004, unit: 'W', plugs: 1 },
    ], 100, '%');
    assert.equal(roundedComponents.deviceLoad, 100);
    assert.equal(roundedComponents.forecastWatt, 100);
    assert.equal(roundedComponents.totalLoad, 200);
    assert.equal(calculations.calculateLoadBreakdown([
        { name: 'Below precision', load: 0.0000004, unit: 'W', plugs: 1 },
    ], 0, '%').valid, false);
    const smallDevice = calculations.calculateLoadBreakdown([
        { name: 'Router', load: 100, unit: 'W', plugs: 1 },
        { name: 'Below precision', load: 0.0000004, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(smallDevice.valid, false);
    assert.deepEqual(smallDevice.precisionInvalidTarget, { type: 'device-load', index: 1 });
    const smallForecast = calculations.calculateLoadBreakdown([
        { name: 'Router', load: 100, unit: 'W', plugs: 1 },
    ], 0.0000004, 'W');
    assert.equal(smallForecast.valid, false);
    assert.deepEqual(smallForecast.precisionInvalidTarget, { type: 'forecast' });

    const displayableRows = calculations.calculateLoadBreakdown([
        { name: 'One', load: 0.0000006, unit: 'W', plugs: 1 },
        { name: 'Two', load: 0.0000006, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(displayableRows.deviceLoad, 0.000002);
    assert.equal(displayableRows.totalLoad, 0.000002);
});

test('rounds load and VA conversion at exact decimal midpoints', () => {
    const belowHalfWatt = calculations.calculateLoadBreakdown([
        { name: 'Below half micro-watt', load: 4.999999999e-7, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(belowHalfWatt.valid, false);
    assert.deepEqual(belowHalfWatt.precisionInvalidTarget, { type: 'device-load', index: 0 });

    const belowHalfVa = calculations.calculateLoadBreakdown([
        { name: 'Below half micro-VA conversion', load: 6.249999999e-7, unit: 'VA', plugs: 1 },
    ], 0, '%');
    assert.equal(belowHalfVa.valid, false);
    assert.deepEqual(belowHalfVa.precisionInvalidTarget, { type: 'device-load', index: 0 });

    const belowRoundUp = calculations.calculateLoadBreakdown([
        { name: 'Below rounding midpoint', load: 1.0000004999999998, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(belowRoundUp.deviceLoad, 1);
    assert.equal(belowRoundUp.totalLoad, 1);

    const exactHalf = calculations.calculateLoadBreakdown([
        { name: 'Exact half micro-watt', load: 0.0000005, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(exactHalf.valid, true);
    assert.equal(exactHalf.totalLoad, 0.000001);

    const belowHalfForecast = calculations.calculateLoadBreakdown([
        { name: 'Forecast base', load: 1000, unit: 'W', plugs: 1 },
    ], 0.00000004999999999, '%');
    assert.equal(belowHalfForecast.valid, false);
    assert.deepEqual(belowHalfForecast.precisionInvalidTarget, { type: 'forecast' });
});

test('rejects decimal string loads and Forecast values lost by Number coercion', () => {
    for (const load of ['9007199254740993', '10000000000000001', '1e-324']) {
        const result = calculations.calculateLoadBreakdown([{ load, unit: 'W', plugs: 1 }], 0, '%');
        assert.equal(result.valid, false, `load ${load} must not be silently changed`);
        assert.deepEqual(result.precisionInvalidTarget, { type: 'device-load', index: 0 });
    }

    const negativeUnderflow = calculations.calculateLoadBreakdown([
        { load: '-1e-324', unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(negativeUnderflow.valid, false);

    const forecast = calculations.calculateLoadBreakdown([
        { load: 100, unit: 'W', plugs: 1 },
    ], '9007199254740993', 'W');
    assert.equal(forecast.valid, false);
    assert.deepEqual(forecast.precisionInvalidTarget, { type: 'forecast' });

    const tinyForecast = calculations.calculateLoadBreakdown([
        { load: 100, unit: 'W', plugs: 1 },
    ], '1e-324', 'W');
    assert.equal(tinyForecast.valid, false);
    assert.deepEqual(tinyForecast.precisionInvalidTarget, { type: 'forecast' });

    const leadingDecimal = calculations.calculateLoadBreakdown([
        { name: 'Browser number format', load: '.5', unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(leadingDecimal.valid, true);
    assert.equal(leadingDecimal.totalLoad, 0.5);

    const leadingDecimalHalf = calculations.calculateLoadBreakdown([
        { name: 'Browser half micro-watt', load: '.0000005', unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(leadingDecimalHalf.valid, true);
    assert.equal(leadingDecimalHalf.totalLoad, 0.000001);
});

test('does not bias whole-number loads by the rounding epsilon at large magnitudes', () => {
    const watts = calculations.calculateLoadBreakdown([
        { name: 'Large load', load: 10_000_000_000, unit: 'W', plugs: 1 },
    ], 20, '%');
    assert.equal(watts.deviceLoad, 10_000_000_000);
    assert.equal(watts.forecastWatt, 2_000_000_000);
    assert.equal(watts.totalLoad, 12_000_000_000);

    const va = calculations.calculateLoadBreakdown([
        { name: 'Large VA load', load: 12_500_000_000, unit: 'VA', plugs: 1 },
    ], 0, '%');
    assert.equal(va.deviceLoad, 10_000_000_000);
    assert.equal(va.totalLoad, 10_000_000_000);
});

test('preserves six-decimal precision when scaled loads exceed the safe integer range', () => {
    for (const load of [9_999_999_999.123457, 10_000_000_000.123457]) {
        const result = calculations.calculateLoadBreakdown([
            { name: 'Fractional large load', load, unit: 'W', plugs: 1 },
        ], 0, '%');
        assert.equal(result.valid, true);
        assert.equal(result.deviceLoad, load);
        assert.equal(result.totalLoad, load);
    }
});

test('sums displayed micro-watt loads exactly or rejects totals outside numeric precision', () => {
    const loads = [
        { name: 'Large device', load: 100_000_000, unit: 'W', plugs: 1 },
        ...Array.from({ length: 1000 }, (_, index) => ({
            name: `Small device ${index + 1}`,
            load: 0.000001,
            unit: 'W',
            plugs: 1,
        })),
    ];
    const sum = calculations.calculateLoadBreakdown(loads, 0, '%');
    assert.equal(sum.valid, true);
    assert.equal(sum.totalLoad, 100_000_000.001);

    const unrepresentable = calculations.calculateLoadBreakdown([
        { name: 'Large device', load: 1_000_000_000_000, unit: 'W', plugs: 1 },
        { name: 'One micro-watt', load: 0.000001, unit: 'W', plugs: 1 },
    ], 0, '%');
    assert.equal(unrepresentable.valid, false);
    assert.deepEqual(unrepresentable.precisionInvalidTarget, { type: 'device-load', index: 1 });
});

test('counts zero-watt device outlets and ignores blank or invalid rows', () => {
    const result = calculations.calculateLoadBreakdown([
        { name: 'Router', load: 100, unit: 'W', plugs: 1 },
        { name: 'Idle', load: 0, unit: 'W', plugs: 2 },
        { name: 'Blank', load: '', unit: 'W', plugs: 4 },
    ], 25, 'W');

    assert.equal(result.deviceLoad, 100);
    assert.equal(result.forecastWatt, 25);
    assert.equal(result.totalLoad, 125);
    assert.equal(result.totalPlugs, 3);
    assert.equal(result.devices.length, 2);
    assert.equal(calculations.calculateLoadBreakdown([{ load: Infinity, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 0.001, unit: 'W', plugs: 1 }], 0, '%').totalLoad, 0.001);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 1e308, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: 1.5 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: Number.MAX_VALUE }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: -1, unit: 'W', plugs: 1 }], 0, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'W', plugs: 1 }], -1, '%').valid, false);
    assert.equal(calculations.calculateLoadBreakdown([{ load: 100, unit: 'watts', plugs: 1 }], 10, 'watts').valid, false);
    assert.equal(calculations.calculateLoadBreakdown(null, 0, '%').valid, false);

    const imported = calculations.normalizeImportData({
        devices: [
            { name: 'Idle', load: 0, unit: 'W', plugs: 2 },
            { name: 'Router', load: 100, unit: 'W', plugs: 1 },
        ],
        calculatedData: {},
    });
    assert.equal(imported.devices.length, 2);
    assert.equal(calculations.calculateLoadBreakdown(imported.devices, 0, '%').totalPlugs, 3);
    const exported = calculations.buildExportData({
        mode: 'enterprise',
        devices: imported.devices,
        calculatedData: { forecastValue: 0, forecastUnit: '%', totalLoad: 100 },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(calculations.normalizeImportData(exported).devices[0].load, 0);
});

test('rejects plug counts whose decimal source is changed by Number conversion', () => {
    const device = { name: 'Router', load: 100, unit: 'W', plugs: '1.00000000000000001' };
    const invalidBreakdown = calculations.calculateLoadBreakdown([device], 0, '%');
    assert.equal(invalidBreakdown.valid, false);
    assert.equal(invalidBreakdown.invalidPlugIndex, 0);
    assert.throws(() => calculations.normalizeImportData({
        devices: [device], calculatedData: {},
    }), { name: 'InvalidImportDataError' });
    const numericJson = calculations.parseImportJson('{"devices":[{"name":"Router","load":100,"unit":"W","plugs":1.00000000000000001}],"calculatedData":{}}');
    assert.throws(() => calculations.normalizeImportData(numericJson), { name: 'InvalidImportDataError' });
    assert.equal(calculations.normalizeImportData({
        devices: [{ ...device, plugs: '1.0' }], calculatedData: {},
    }).devices[0].plugs, 1);
});

test('calculates required Ah and rejects invalid sizing inputs', () => {
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 85), 11.764706);
    assert.equal(calculations.calculateBatteryAh(0.01, 1, 48, 100), 0.000004);
    assert.equal(calculations.calculateBatteryAh(0.006, 1, 100, 100), 0.000001);
    assert.equal(calculations.calculateBatteryAh(0.00006, 1, 100, 100), null);
    assert.equal(calculations.calculateBatteryAh(120, '1e-320', 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(0, 60, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, -1, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 0), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 49), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 50), 20);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, 100), 10);
    assert.equal(calculations.calculateBatteryAh(3, 63, 12, 60), 0.4375);
    assert.equal(calculations.calculateBatteryAh(1, 162, 12, 96), 0.234375);
    assert.equal(calculations.calculateBatteryAh('9007199254740993', 60, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, '60.000000000000001', 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(120, 60, 12, '100.000000000000001'), null);
    assert.equal(calculations.calculateBatteryAh(Infinity, 60, 12, 85), null);
    assert.equal(calculations.calculateBatteryAh(1e308, 1e308, 1e308, 100), null);
    assert.equal(calculations.calculateBatteryAh(1e308, 1e-308, 1e-308, 100), null);
});

test('identifies Ah requirements below the supported 1 microamp-hour resolution', () => {
    assert.equal(calculations.isBelowAhResolution(0.00001, 1, 12, 100), true);
    assert.equal(calculations.isBelowAhResolution(0.006, 1, 100, 100), false);
    assert.equal(calculations.isBelowAhResolution(0.003, 1, 100, 100), true);
    assert.equal(calculations.isBelowAhResolution(120, 0.5, 12, 85), false);
});

test('identifies valid Ah calculations outside the supported output range', () => {
    assert.equal(calculations.calculateBatteryAh(1e12, 60, 12, 85), null);
    assert.equal(calculations.isAhRequirementBeyondOutputRange(1e12, 60, 12, 85), true);
    assert.equal(calculations.isAhRequirementBeyondOutputRange(0.00001, 1, 12, 100), false);
    assert.equal(calculations.isAhRequirementBeyondOutputRange(120, 60, 12, 85), false);
    assert.equal(calculations.isAhRequirementBeyondOutputRange(-1, 60, 12, 85), false);
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
    assert.equal(calculations.calculateRuntimeMinutes(1, 12, 60, 128), 3.38);
    assert.equal(calculations.calculateRuntimeMinutes(1, 12, 85, 800), 0.77);
    assert.equal(calculations.calculateRuntimeMinutes('9007199254740993', 12, 85, 100), null);
    assert.equal(calculations.calculateRuntimeMinutes(11.76, 12, 85, '100.000000000000001'), null);
    assert.ok(Math.abs(calculations.calculateRuntimeMinutes(1 / 6000, 12, 100, 120) - 0.001) < 1e-12);
    assert.equal(calculations.calculateRuntimeMinutes(Number.MIN_VALUE, 12, 100, 2000), null);
    assert.equal(calculations.calculateRuntimeMinutes(1e306, 1e308, 50, 1e308), null);
    assert.equal(calculations.calculateRuntimeMinutes(1e306, 1e-308, 50, 1e-308), null);
});

test('keeps Ah sizing conservative and inverse Runtime within display precision across a supported input grid', () => {
    const loadsW = [10, 45, 120, 750];
    const backupMinutes = [15, 30, 60, 240];
    const voltages = [12, 24, 48];
    const efficiencies = [50, 85, 100];

    for (const loadW of loadsW) {
        for (const minutes of backupMinutes) {
            for (const voltage of voltages) {
                for (const efficiency of efficiencies) {
                    const exactAh = loadW * (minutes / 60) / (voltage * (efficiency / 100));
                    const requiredAh = calculations.calculateBatteryAh(loadW, minutes, voltage, efficiency);
                    assert.ok(requiredAh >= exactAh - 1e-12, `Ah must not under-size ${loadW} W / ${minutes} min / ${voltage} V / ${efficiency}%`);
                    assert.ok(requiredAh - exactAh < 0.00000101, 'Ah rounding should remain within one micro-Ah');

                    const reproducedMinutes = calculations.calculateRuntimeMinutes(requiredAh, voltage, efficiency, loadW);
                    assert.ok(Math.abs(reproducedMinutes - minutes) <= 0.01, `inverse Runtime must agree for ${loadW} W / ${minutes} min / ${voltage} V / ${efficiency}%`);
                }
            }
        }
    }
});

test('builds a unique numeric runtime curve that matches the exact current load', () => {
    const curve = calculations.buildRuntimeCurve(11.76, 12, 85, 100);
    const loads = curve.map(point => point.loadW);
    assert.deepEqual(loads, [50, 100, 150, 200, 250, 300]);
    assert.equal(new Set(loads).size, loads.length);
    assert.equal(curve.find(point => point.currentLoad).runtimeMinutes, 71.97);

    const fractional = calculations.buildRuntimeCurve(11.76, 12, 85, 105.6);
    assert.ok(fractional.some(point => point.loadW === 105.6 && point.currentLoad));

    assert.deepEqual(calculations.buildRuntimeCurve(11.76, 12, 85, '9007199254740993'), []);
    assert.deepEqual(calculations.buildRuntimeCurve('9007199254740993', 12, 85, 100), []);
    assert.deepEqual(calculations.buildRuntimeCurve(11.76, 12, 150, 100), []);

    const boundedCurve = calculations.buildRuntimeCurve('1e20', 12, 85, 100_000_000);
    assert.ok(boundedCurve.length > 0);
    assert.ok(boundedCurve.every(point => Number.isFinite(point.runtimeMinutes)));
    assert.ok(!boundedCurve.some(point => point.loadW === 50));
});

test('parses manufacturer runtime CSV into validated load/runtime points', () => {
    assert.equal(typeof calculations.parseRuntimeCurveCsv, 'function');
    const points = calculations.parseRuntimeCurveCsv('\uFEFFload_w,runtime_minutes\n100,60\n200,30\n400,15\n');
    assert.deepEqual(points, [
        { loadW: 100, runtimeMinutes: 60 },
        { loadW: 200, runtimeMinutes: 30 },
        { loadW: 400, runtimeMinutes: 15 },
    ]);
    assert.throws(() => calculations.parseRuntimeCurveCsv('load_w,runtime_minutes\n100,60\n'), { name: 'InvalidRuntimeCurveError' });
    assert.throws(() => calculations.parseRuntimeCurveCsv('load_w,runtime_minutes\n100,60\n100,30\n200,15'), { name: 'InvalidRuntimeCurveError' });
    assert.throws(() => calculations.parseRuntimeCurveCsv('load_w,runtime_minutes\n100,30\n200,60'), { name: 'InvalidRuntimeCurveError' });
    assert.throws(() => calculations.parseRuntimeCurveCsv('load_w,runtime_minutes\n9007199254740993,60\n200,30'), { name: 'InvalidRuntimeCurveError' });
    assert.throws(() => calculations.parseRuntimeCurveCsv('load_w,runtime_minutes\n1,2e21\n2,1e21'), { name: 'InvalidRuntimeCurveError' });
});

test('interpolates manufacturer runtime only within published load points', () => {
    assert.equal(typeof calculations.calculateRuntimeFromCurve, 'function');
    const points = [
        { loadW: 100, runtimeMinutes: 60 },
        { loadW: 200, runtimeMinutes: 30 },
        { loadW: 400, runtimeMinutes: 15 },
    ];
    assert.equal(calculations.calculateRuntimeFromCurve(points, 100), 60);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 150), 45);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 250), 26.25);
    assert.equal(calculations.calculateRuntimeFromCurve([
        { loadW: 5041, runtimeMinutes: 5312 },
        { loadW: 5065, runtimeMinutes: 1826 },
    ], 5042.1), 5152.23);
    assert.equal(calculations.calculateRuntimeFromCurve([
        { loadW: 1, runtimeMinutes: 2e21 },
        { loadW: 2, runtimeMinutes: 1e21 },
    ], 1.5), null);
    assert.equal(calculations.calculateRuntimeFromCurve([
        { loadW: 1, runtimeMinutes: '1e-300' },
        { loadW: 2, runtimeMinutes: '5e-301' },
    ], 1.5), 7.5e-301);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 99.999), null);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 400.001), null);
    assert.equal(calculations.calculateRuntimeFromCurve(points, '9007199254740993'), null);
});

test('matches published Vertiv GXTRT-1000 internal-battery runtime points', () => {
    // Vertiv Installer/User Guide, Table 6-3, p.30, internal battery only:
    // https://www.vertiv.com/491490/globalassets/products/critical-power/uninterruptible-power-supplies-ups/vertiv-liebert-gxt-rt-ups/liebert-gxt-rt-1-3kva-230v-user-guide-r3.pdf
    const points = calculations.parseRuntimeCurveCsv([
        'load_w,runtime_minutes',
        '225,19', '450,9', '621,5.3', '675,5', '720,3.5', '810,3.5', '900,3.1',
    ].join('\n'));
    assert.deepEqual(points, [
        { loadW: 225, runtimeMinutes: 19 },
        { loadW: 450, runtimeMinutes: 9 },
        { loadW: 621, runtimeMinutes: 5.3 },
        { loadW: 675, runtimeMinutes: 5 },
        { loadW: 720, runtimeMinutes: 3.5 },
        { loadW: 810, runtimeMinutes: 3.5 },
        { loadW: 900, runtimeMinutes: 3.1 },
    ]);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 450), 9);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 337.5), 14);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 224.99), null);
    assert.equal(calculations.calculateRuntimeFromCurve(points, 900.01), null);
});

test('accepts quoted numeric values in manufacturer Runtime CSV files', () => {
    assert.deepEqual(calculations.parseRuntimeCurveCsv([
        'load_w,runtime_minutes',
        '"100","60"',
        '"200","30"',
    ].join('\n')), [
        { loadW: 100, runtimeMinutes: 60 },
        { loadW: 200, runtimeMinutes: 30 },
    ]);
});

test('enforces the Runtime CSV limit in UTF-8 bytes, not JavaScript string units', () => {
    const csv = `load_w,runtime_minutes\n"100${'　'.repeat(90_000)}","60"\n"200","30"`;
    assert.ok(csv.length < 256_000);
    assert.ok(Buffer.byteLength(csv, 'utf8') > 256_000);
    assert.throws(() => calculations.parseRuntimeCurveCsv(csv), { name: 'InvalidRuntimeCurveError' });
});

test('detects edits that no longer match an imported Runtime curve provenance', () => {
    const curve = {
        model: 'UPS Model A', batteryConfig: 'Internal', source: 'manual.pdf', conditions: '25 C',
        points: [{ loadW: 100, runtimeMinutes: 60 }, { loadW: 200, runtimeMinutes: 30 }],
    };
    assert.equal(calculations.runtimeCurveMetadataMatches(curve, {
        model: 'UPS Model A', batteryConfig: 'Internal', source: 'manual.pdf', conditions: '25 C',
    }), true);
    assert.equal(calculations.runtimeCurveMetadataMatches(curve, {
        model: 'UPS Model B', batteryConfig: 'Internal', source: 'manual.pdf', conditions: '25 C',
    }), false);
});

test('builds a manufacturer curve with a marked interpolated current-load point', () => {
    assert.equal(typeof calculations.buildManufacturerRuntimeCurve, 'function');
    const points = [
        { loadW: 100, runtimeMinutes: 60 },
        { loadW: 200, runtimeMinutes: 30 },
        { loadW: 400, runtimeMinutes: 15 },
    ];
    const curve = calculations.buildManufacturerRuntimeCurve(points, 150);
    assert.deepEqual(curve.map(point => point.loadW), [100, 150, 200, 400]);
    assert.equal(curve.find(point => point.currentLoad).runtimeMinutes, 45);
    assert.deepEqual(calculations.buildManufacturerRuntimeCurve(points, 99), []);
});

test('explains which curve remains active after a failed CSV import', () => {
    assert.equal(typeof calculations.runtimeCurveImportFailureMessage, 'function');
    const error = new Error('CSV format error');
    assert.match(calculations.runtimeCurveImportFailureMessage(error, 'UPS Model X'), /UPS Model X/);
    assert.match(calculations.runtimeCurveImportFailureMessage(error, null), /ยังไม่มีเส้นโค้งที่ใช้งาน/);
});

test('validates and preserves manufacturer runtime curve provenance in imports', () => {
    const runtimeCurve = {
        model: 'UPS-Example 1000',
        batteryConfig: 'Internal battery pack',
        source: 'https://vendor.example/runtime-chart.pdf',
        conditions: 'Fully charged; 25 C',
        points: [
            { loadW: 100, runtimeMinutes: 60 },
            { loadW: 200, runtimeMinutes: 30 },
        ],
    };
    const imported = calculations.normalizeImportData({
        mode: 'enterprise', runtimeSource: 'manufacturer', runtimeCurve,
        devices: [{ name: 'Router', load: 150, unit: 'W', plugs: 1 }], calculatedData: {},
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(imported.runtimeSource, 'manufacturer');
    assert.deepEqual(imported.runtimeCurve, runtimeCurve);
    assert.throws(() => calculations.normalizeImportData({
        mode: 'enterprise', runtimeSource: 'manufacturer', runtimeCurve,
        devices: [{ name: 'Router', load: 99, unit: 'W', plugs: 1 }], calculatedData: {},
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    }), { name: 'InvalidImportDataError' });
});

test('calculates PF-based UPS VA screening and validates outlet boundaries', () => {
    assert.equal(calculations.recommendUpsVA(76.92), 200);
    assert.equal(calculations.recommendUpsVA(77), 200);
    assert.equal(calculations.recommendUpsVA(200), 400);
    assert.equal(calculations.recommendUpsVA(61.53846153846154), 200);
    assert.equal(calculations.recommendUpsVA(615.3846153846154), 1100);
    assert.equal(calculations.recommendUpsVA('61.53846153846154'), 200);
    assert.equal(calculations.recommendUpsVA('9007199254740993'), null);
    assert.equal(calculations.recommendUpsVA(0), 0);
    assert.equal(calculations.recommendUpsVA(1.4e308), null);
    assert.equal(calculations.recommendUpsVA(1e308), null);
    assert.equal(calculations.recommendUpsVA(1.001e20), null);
    assert.deepEqual(calculations.planOutlets(4), { requiredUpsCount: 1, outletsPerUps: 4 });
    assert.deepEqual(calculations.planOutlets(5), { requiredUpsCount: 1, outletsPerUps: 6 });
    assert.deepEqual(calculations.planOutlets(12), { requiredUpsCount: 1, outletsPerUps: 12 });
    assert.deepEqual(calculations.planOutlets(13), { requiredUpsCount: 2, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(24), { requiredUpsCount: 2, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(25), { requiredUpsCount: 3, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(4.5), { requiredUpsCount: 0, outletsPerUps: 0 });
    assert.deepEqual(calculations.planOutlets(-1), { requiredUpsCount: 0, outletsPerUps: 0 });
});

test('does not infer UPS topology from load watts alone', () => {
    assert.equal(calculations.recommendUpsType, undefined);
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
    for (const backupTime of [0.5, '1e-320']) {
        assert.throws(() => calculations.normalizeImportData({
            devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }], calculatedData: {},
            settings: { backupTime, voltage: 12, efficiency: 85 },
        }), { name: 'InvalidImportDataError' });
    }
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
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Below runtime range', load: 2000, unit: 'W', plugs: 1 }], calculatedData: {},
        settings: { backupTime: 60, voltage: 12, efficiency: 100, runtimeBatteryAh: Number.MIN_VALUE },
    }), { name: 'InvalidImportDataError' });
    for (const load of ['9007199254740993', '1e-324', '-1e-324']) {
        assert.throws(() => calculations.normalizeImportData({
            devices: [{ name: 'Precision edge', load, unit: 'W', plugs: 1 }], calculatedData: {},
        }), { name: 'InvalidImportDataError' });
    }
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Router', load: 100, unit: 'W', plugs: 1 }], calculatedData: {},
        forecast: { value: '9007199254740993', unit: 'W' },
    }), { name: 'InvalidImportDataError' });
    assert.throws(() => calculations.normalizeImportData({
        devices: [{ name: 'Router', load: 100, unit: 'W', plugs: 1 }], calculatedData: {},
        forecast: { value: '1e-324', unit: 'W' },
    }), { name: 'InvalidImportDataError' });
    const numericTextImport = calculations.normalizeImportData({
        devices: [{ name: 'Router', load: '120.0', unit: 'W', plugs: 1 }], calculatedData: {},
        forecast: { value: '10.0', unit: '%' },
    });
    assert.equal(numericTextImport.devices[0].load, 120);
    assert.equal(numericTextImport.forecast.value, 10);
    for (const settings of [
        { voltage: '12.000000000000001' },
        { efficiency: '100.000000000000001' },
        { backupTime: '60.000000000000001' },
        { runtimeBatteryAh: '9007199254740993' },
    ]) {
        assert.throws(() => calculations.normalizeImportData({
            devices: [{ name: 'Router', load: 100, unit: 'W', plugs: 1 }], calculatedData: {}, settings,
        }), { name: 'InvalidImportDataError' });
    }
});

test('preserves raw JSON numeric tokens so import can reject precision-losing values', () => {
    const parsed = calculations.parseImportJson(`{
        "devices": [{"name":"Boundary","load":1.00000049999999999,"unit":"W","plugs":1}],
        "calculatedData": {},
        "forecast": {"value": 0, "unit": "%"}
    }`);
    assert.equal(parsed.devices[0].load, '1.00000049999999999');
    assert.throws(() => calculations.normalizeImportData(parsed), { name: 'InvalidImportDataError' });
    assert.equal(calculations.parseImportJson('{"name":"1.00000049999999999","value":120}').name, '1.00000049999999999');
    assert.equal(calculations.parseImportJson('\uFEFF{"name":"BOM","value":120}').name, 'BOM');
    assert.throws(() => calculations.parseImportJson('{"value":01}'), { name: 'SyntaxError' });
    assert.throws(() => calculations.parseImportJson(' '.repeat(calculations.MAX_IMPORT_FILE_BYTES + 1)), { name: 'InvalidImportDataError' });
    const utf8Oversize = `{"padding":"${'😀'.repeat(260_000)}"}`;
    assert.ok(utf8Oversize.length < calculations.MAX_IMPORT_FILE_BYTES);
    assert.ok(Buffer.byteLength(utf8Oversize, 'utf8') > calculations.MAX_IMPORT_FILE_BYTES);
    assert.throws(() => calculations.parseImportJson(utf8Oversize), { name: 'InvalidImportDataError' });
});

test('rejects JSON imports above the supported device count', () => {
    const data = {
        devices: Array.from({ length: 501 }, (_, index) => ({ name: `Device ${index}`, load: 1, unit: 'W', plugs: 1 })),
        calculatedData: {},
    };
    assert.throws(() => calculations.normalizeImportData(data), { name: 'InvalidImportDataError' });
    data.devices.pop();
    assert.equal(calculations.normalizeImportData(data).devices.length, calculations.MAX_DEVICES);
});

test('bounds device names and keeps maximum supported exports importable', () => {
    const device = { name: 'x'.repeat(calculations.MAX_DEVICE_NAME_LENGTH + 1), load: 1, unit: 'W', plugs: 1 };
    assert.throws(() => calculations.normalizeImportData({ devices: [device], calculatedData: {} }), {
        name: 'InvalidImportDataError', message: /ชื่ออุปกรณ์ยาวเกิน/,
    });
    device.name = 'x'.repeat(calculations.MAX_DEVICE_NAME_LENGTH);
    assert.equal(calculations.normalizeImportData({ devices: [device], calculatedData: {} }).devices[0].name.length, calculations.MAX_DEVICE_NAME_LENGTH);

    const curve = {
        model: 'ม'.repeat(160), batteryConfig: 'บ'.repeat(240), source: 'แหล่ง'.repeat(409), conditions: '😀'.repeat(512),
        points: Array.from({ length: 1000 }, (_, index) => ({ loadW: index + 1, runtimeMinutes: 1001 - index })),
    };
    const exported = calculations.buildExportData({
        mode: 'enterprise', runtimeSource: 'manufacturer', runtimeCurve: curve,
        devices: Array.from({ length: calculations.MAX_DEVICES }, (_, index) => ({
            name: '😀'.repeat(calculations.MAX_DEVICE_NAME_LENGTH / 2), load: 1, unit: 'W', plugs: 1,
        })),
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    const serialized = JSON.stringify(exported);
    assert.ok(serialized.length <= calculations.MAX_IMPORT_FILE_BYTES);
    assert.ok(Buffer.byteLength(serialized, 'utf8') <= calculations.MAX_IMPORT_FILE_BYTES);
    assert.equal(calculations.normalizeImportData(calculations.parseImportJson(serialized)).devices.length, calculations.MAX_DEVICES);
});

test('rejects import schema versions newer than the supported format', () => {
    assert.throws(() => calculations.normalizeImportData({
        version: '99.0',
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: {},
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
    const imported = calculations.normalizeImportData(calculations.parseImportJson(JSON.stringify(exported)));
    assert.equal(exported.exportDate, date.toISOString());
    assert.equal(imported.devices[0].unit, 'VA');
    assert.equal(imported.settings.runtimeBatteryAh, 30);
    assert.deepEqual(imported.forecast, { value: 10, unit: '%' });
});

test('exports battery sizing and Runtime recalculated from the latest form settings', () => {
    const exported = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: {
            deviceLoad: 120, totalLoad: 120, totalPlugs: 1,
            forecastValue: 0, forecastUnit: '%', forecastWatt: 0,
            batteryAh: 11.764706, backupTime: 60, voltage: 12, efficiency: 85, runtime: 60,
        },
        settings: { backupTime: 30, voltage: 24, efficiency: 90, runtimeBatteryAh: null },
        calculationSettings: { backupTime: 30, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });

    assert.equal(exported.calculatedData.batteryAh, 5.882353);
    assert.equal(exported.calculatedData.backupTime, 30);
    assert.equal(exported.calculatedData.voltage, 12);
    assert.equal(exported.calculatedData.efficiency, 85);
    assert.equal(exported.calculatedData.runtime, 30);
    assert.equal(exported.settings.voltage, 24);
    assert.equal(exported.settings.efficiency, 90);

    const manualCapacityExport = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: exported.calculatedData,
        settings: { backupTime: 30, voltage: 24, efficiency: 90, runtimeBatteryAh: 11.76 },
        calculationSettings: { backupTime: 30, voltage: 12, efficiency: 85, runtimeBatteryAh: 11.76 },
    });
    assert.equal(manualCapacityExport.calculatedData.batteryAh, 5.882353);
    assert.equal(manualCapacityExport.calculatedData.runtime, 59.98);

    const precisionChangedExport = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%', totalLoad: 120 },
        settings: { backupTime: '60.000000000000001', voltage: 12, efficiency: 85, runtimeBatteryAh: null },
        calculationSettings: { backupTime: '60.000000000000001', voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.throws(() => calculations.normalizeImportData(precisionChangedExport), { name: 'InvalidImportDataError' });
});

test('exports and recalculates manufacturer Runtime from the imported curve', () => {
    const runtimeCurve = {
        model: 'UPS-Example 1000', batteryConfig: 'Internal battery pack',
        source: 'https://vendor.example/runtime-chart.pdf', conditions: 'Fully charged; 25 C',
        points: [{ loadW: 100, runtimeMinutes: 60 }, { loadW: 200, runtimeMinutes: 30 }],
    };
    const exported = calculations.buildExportData({
        mode: 'enterprise', runtimeSource: 'manufacturer', runtimeCurve,
        devices: [{ name: 'Server', load: 150, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%', totalLoad: 150 },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(exported.runtimeSource, 'manufacturer');
    assert.deepEqual(exported.runtimeCurve, runtimeCurve);
    assert.equal(exported.calculatedData.runtime, 45);
    const imported = calculations.normalizeImportData(exported);
    assert.equal(imported.runtimeSource, 'manufacturer');
    assert.deepEqual(imported.runtimeCurve, runtimeCurve);
});

test('manufacturer Runtime export without a mode round-trips as Enterprise', () => {
    const exported = calculations.buildExportData({
        runtimeSource: 'manufacturer',
        runtimeCurve: {
            model: 'UPS-Example 1000', batteryConfig: 'Internal battery pack',
            source: 'https://vendor.example/runtime-chart.pdf', conditions: '',
            points: [{ loadW: 100, runtimeMinutes: 60 }, { loadW: 200, runtimeMinutes: 30 }],
        },
        devices: [{ name: 'Server', load: 150, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(exported.mode, 'enterprise');
    assert.equal(calculations.normalizeImportData(exported).runtimeSource, 'manufacturer');
});

test('preserves auto-filled Runtime capacity as automatic across export and import', () => {
    const exported = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%', batteryAh: 0, runtime: null },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: 11.764706 },
        calculationSettings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: 11.764706 },
        runtimeBatteryWasAutoFilled: true,
    });

    assert.equal(exported.settings.runtimeBatteryAh, null);
    assert.equal(exported.calculatedData.runtime, 60);
    assert.equal(calculations.normalizeImportData(exported).settings.runtimeBatteryAh, null);
});

test('preserves Basic or Enterprise mode across export and import', () => {
    for (const mode of ['basic', 'enterprise']) {
        const exported = calculations.buildExportData({
            mode,
            devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
            calculatedData: { forecastValue: 0, forecastUnit: '%', totalLoad: 120 },
            settings: { backupTime: 30, voltage: 24, efficiency: 90, runtimeBatteryAh: null },
            calculationSettings: {
                backupTime: 30,
                voltage: mode === 'basic' ? 12 : 24,
                efficiency: mode === 'basic' ? 85 : 90,
                runtimeBatteryAh: null,
            },
        });

        assert.equal(exported.version, '1.5');
        assert.equal(exported.mode, mode);
        assert.equal(calculations.normalizeImportData(exported).mode, mode);
    }

    assert.equal(calculations.normalizeImportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: {},
    }).mode, 'basic');

    assert.throws(() => calculations.normalizeImportData({
        mode: 'workstation',
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: {},
    }), { name: 'InvalidImportDataError' });
});

test('exports formula snapshots without a supplied mode as explicitly Basic', () => {
    const exported = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(exported.mode, 'basic');
    assert.equal(calculations.normalizeImportData(exported).mode, 'basic');

    const advanced = calculations.buildExportData({
        devices: [{ name: 'Router', load: 120, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 60, voltage: 24, efficiency: 90, runtimeBatteryAh: null },
        calculationSettings: { backupTime: 60, voltage: 24, efficiency: 90, runtimeBatteryAh: null },
    });
    assert.equal(advanced.mode, 'enterprise');
    assert.equal(calculations.normalizeImportData(advanced).mode, 'enterprise');
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

    const partialLegacyForecast = calculations.normalizeImportData({
        devices: [{ name: 'Router', load: 100, unit: 'W' }],
        calculatedData: { forecastWatt: 10 },
        forecast: { watt: 10 },
    });
    assert.deepEqual(partialLegacyForecast.forecast, { value: 10, unit: 'W' });
    assert.equal(calculations.calculateLoadBreakdown(partialLegacyForecast.devices, partialLegacyForecast.forecast.value, partialLegacyForecast.forecast.unit).totalLoad, 110);

    const legacyEnterpriseSettings = calculations.normalizeImportData({
        version: '1.2',
        devices: [{ name: 'Router', load: 120, unit: 'W' }],
        calculatedData: { backupTime: 60, voltage: 24, efficiency: 90 },
        settings: { backupTime: 60, voltage: 24, efficiency: 90 },
    });
    assert.equal(legacyEnterpriseSettings.mode, 'enterprise');

    const legacyBasicWithSavedEnterprisePreferences = calculations.normalizeImportData({
        version: '1.2',
        devices: [{ name: 'Router', load: 120, unit: 'W' }],
        calculatedData: { backupTime: 60, voltage: 12, efficiency: 85 },
        settings: { backupTime: 60, voltage: 24, efficiency: 90 },
    });
    assert.equal(legacyBasicWithSavedEnterprisePreferences.mode, 'basic');
    assert.equal(legacyBasicWithSavedEnterprisePreferences.settings.voltage, 24);
    assert.equal(legacyBasicWithSavedEnterprisePreferences.settings.efficiency, 90);

    const legacyEnterpriseCalculationWithDefaultSettings = calculations.normalizeImportData({
        version: '1.2',
        devices: [{ name: 'Router', load: 120, unit: 'W' }],
        calculatedData: { backupTime: 60, voltage: 24, efficiency: 90 },
        settings: { backupTime: 60, voltage: 12, efficiency: 85 },
    });
    assert.equal(legacyEnterpriseCalculationWithDefaultSettings.mode, 'enterprise');
    assert.equal(legacyEnterpriseCalculationWithDefaultSettings.settings.voltage, 24);
    assert.equal(legacyEnterpriseCalculationWithDefaultSettings.settings.efficiency, 90);
});

test('exports a Basic-mode snapshot with formula Runtime even if an inactive curve is supplied', () => {
    const exported = calculations.buildExportData({
        mode: 'basic', runtimeSource: 'manufacturer',
        runtimeCurve: {
            model: 'UPS-Example', batteryConfig: 'Internal', source: 'test chart', conditions: '',
            points: [{ loadW: 1, runtimeMinutes: 100 }, { loadW: 200, runtimeMinutes: 1 }],
        },
        devices: [{ name: 'Router', load: 100, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
        calculationSettings: { backupTime: 60, voltage: 12, efficiency: 85, runtimeBatteryAh: null },
    });
    assert.equal(exported.runtimeSource, 'formula');
    assert.equal(exported.calculatedData.runtime, 60);
    assert.equal(calculations.normalizeImportData(exported).runtimeSource, 'formula');
});

test('uses Basic calculation assumptions while preserving saved Enterprise preferences', () => {
    const settings = { voltage: 24, efficiency: 90, backupTime: 60 };

    assert.deepEqual(calculations.resolveActiveBatterySettings('basic', settings), {
        voltage: 12, efficiency: 85,
    });
    assert.deepEqual(calculations.resolveActiveBatterySettings('enterprise', settings), {
        voltage: 24, efficiency: 90,
    });
    assert.deepEqual(settings, { voltage: 24, efficiency: 90, backupTime: 60 });
});

test('validates imported Basic Ah using active assumptions instead of saved Enterprise preferences', () => {
    const payload = (load, voltage, efficiency) => ({
        mode: 'basic',
        devices: [{ name: 'Resolution boundary', load, unit: 'W', plugs: 1 }],
        calculatedData: { voltage: 12, efficiency: 85 },
        settings: { backupTime: 60, voltage, efficiency },
        forecast: { value: 0, unit: '%' },
    });
    const accepted = calculations.normalizeImportData(payload(0.00002, 48, 100));
    assert.equal(accepted.mode, 'basic');
    assert.equal(accepted.settings.voltage, 48);
    assert.throws(() => calculations.normalizeImportData(payload(0.000007, 12, 50)), {
        name: 'InvalidImportDataError',
        message: 'ค่าความจุที่ต้องการต่ำกว่า 0.000001 Ah ซึ่งเป็นความละเอียดขั้นต่ำที่รองรับ',
    });
});

test('exports Basic results with 12 V and 85 percent while retaining Enterprise settings', () => {
    const payload = {
        mode: 'basic',
        devices: [{ name: 'Load', load: 100, unit: 'W', plugs: 1 }],
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        settings: { backupTime: 15, voltage: 24, efficiency: 90, runtimeBatteryAh: 10 },
    };
    for (const extra of [{}, { calculationSettings: payload.settings }]) {
        const exported = calculations.buildExportData({ ...payload, ...extra });
        assert.equal(exported.calculatedData.voltage, 12);
        assert.equal(exported.calculatedData.efficiency, 85);
        assert.equal(exported.calculatedData.batteryAh, 2.450981);
        assert.equal(exported.calculatedData.runtime, 61.2);
        assert.equal(exported.settings.voltage, 24);
        assert.equal(exported.settings.efficiency, 90);
        assert.equal(calculations.normalizeImportData(exported).mode, 'basic');
    }
});

test('keeps imported chart interpolation on the source segment before display rounding', () => {
    for (const [lower, upper, midpoint] of [
        [1.004, 1.003, 1.0035],
        [0.009, 0.006, 0.0075],
        [1e-320, 5e-324, 5.005e-321],
        [1e20, 1e-320, 5e19],
    ]) {
        const points = [{ loadW: 100, runtimeMinutes: lower }, { loadW: 200, runtimeMinutes: upper }];
        const curve = calculations.buildManufacturerRuntimeCurve(points, 150);
        assert.equal(curve[1].runtimeMinutes, midpoint);
        assert.ok(curve[0].runtimeMinutes >= curve[1].runtimeMinutes);
        assert.ok(curve[1].runtimeMinutes >= curve[2].runtimeMinutes);
        assert.equal(curve[0].runtimeMinutes, lower);
        assert.equal(curve[2].runtimeMinutes, upper);
    }
});

test('retains valid imported Runtime when exact hundredths exceed Number precision', () => {
    const points = [{ loadW: 1, runtimeMinutes: 1e20 }, { loadW: 2, runtimeMinutes: 1.003 }];
    assert.equal(calculations.calculateRuntimeFromCurve(points, 1.5), 5e19);
    assert.equal(calculations.buildManufacturerRuntimeCurve(points, 1.5)[1].runtimeMinutes, 5e19);
});

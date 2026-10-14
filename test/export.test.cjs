const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const calculations = require('../calculations.js');

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const exportSource = html.slice(html.indexOf('async function exportToJSON()'), html.indexOf('// Import from JSON'));

function exportFixture(showSaveFilePicker, devices = [{ name: 'Router ทดสอบ 🔋', load: 120, unit: 'W', plugs: 1 }]) {
    const events = [], alerts = [], deferred = [], anchors = [];
    const values = { backupTimeMin: '60', batteryVoltage: '12', upsEfficiency: '85', runtimeBatteryAh: '10' };
    const context = vm.createContext({
        UPScalculations: calculations,
        currentMode: 'basic',
        devices,
        calculatedData: { forecastValue: 0, forecastUnit: '%' },
        enterpriseBatterySettings: { voltage: 12, efficiency: 85 },
        runtimeBatteryWasAutoFilled: false, runtimeSource: 'formula', runtimeCurve: null,
        loadInputValid: true, updateTotalLoad() {},
        window: { showSaveFilePicker },
        document: {
            getElementById(id) { return { value: values[id] }; },
            createElement() {
                const link = { style: {}, click() { events.push('download'); } };
                anchors.push(link);
                return link;
            },
            body: { appendChild() {}, removeChild() { events.push('removed'); } },
        },
        alert(message) { alerts.push(message); },
        console: { warn() {} },
        setTimeout(callback) { deferred.push(callback); },
    });
    return { run: vm.runInContext(`(${exportSource})`, context), events, alerts, deferred, anchors };
}

test('export cancellation does not download or announce a saved file', async () => {
    const fixture = exportFixture(async () => { throw Object.assign(new Error('cancel'), { name: 'AbortError' }); });
    await fixture.run();
    assert.deepEqual(fixture.events, []);
    assert.deepEqual(fixture.alerts, []);
});

test('export announces native save only after writing and closing valid JSON', async () => {
    let text;
    const fixture = exportFixture(async () => ({ createWritable: async () => ({
        async write(value) { text = value; fixture.events.push('write'); },
        async close() { fixture.events.push('close'); },
    }) }));
    await fixture.run();
    assert.deepEqual(fixture.events, ['write', 'close']);
    assert.equal(fixture.alerts.length, 1);
    assert.match(fixture.alerts[0], /บันทึกไฟล์สำเร็จ/);
    const restored = calculations.normalizeImportData(calculations.parseImportJson(text));
    assert.equal(restored.devices[0].load, 120);
    assert.equal(restored.settings.runtimeBatteryAh, 10);
});

test('export saves the device quantity in schema 1.6 and restores its calculated load', async () => {
    const fixture = exportFixture(undefined, [
        { name: 'Router ทดสอบ 🔋', load: 120, unit: 'W', quantity: 3, plugs: 1 },
    ]);
    await fixture.run();

    const encodedPayload = fixture.anchors[0].href.slice('data:application/json;charset=utf-8,'.length);
    const exported = JSON.parse(decodeURIComponent(encodedPayload));
    assert.equal(exported.version, '1.6');
    assert.equal(exported.devices[0].quantity, 3);
    assert.equal(exported.calculatedData.deviceLoad, 360);

    const restored = calculations.normalizeImportData(exported);
    assert.equal(restored.devices[0].quantity, 3);
    assert.equal(calculations.calculateLoadBreakdown(restored.devices, 0, '%').deviceLoad, 360);
});

test('export aborts a failed writable before starting its download fallback', async () => {
    const fixture = exportFixture(async () => ({ createWritable: async () => ({
        async write() { fixture.events.push('write'); throw new Error('disk write failed'); },
        async close() { fixture.events.push('close'); },
        async abort() { fixture.events.push('abort'); },
    }) }));
    await fixture.run();
    assert.deepEqual(fixture.events, ['write', 'abort', 'download']);
    assert.match(fixture.alerts[0], /กำลังดาวน์โหลด/);
    assert.ok(!fixture.alerts.some(message => message.includes('บันทึกไฟล์สำเร็จ')));
    fixture.deferred.forEach(callback => callback());
    assert.equal(fixture.events.at(-1), 'removed');
});

test('failed close and cleanup do not announce native save or suppress fallback', async () => {
    const fixture = exportFixture(async () => ({ createWritable: async () => ({
        async write() { fixture.events.push('write'); },
        async close() { fixture.events.push('close'); throw new Error('close failed'); },
        async abort() { fixture.events.push('abort'); throw new Error('already errored'); },
    }) }));
    await fixture.run();
    assert.deepEqual(fixture.events, ['write', 'close', 'abort', 'download']);
    assert.equal(fixture.alerts.length, 1);
    assert.match(fixture.alerts[0], /กำลังดาวน์โหลด/);
});

test('download fallback preserves Thai text and an importable JSON payload', async () => {
    const fixture = exportFixture(undefined);
    await fixture.run();
    assert.deepEqual(fixture.events, ['download']);
    const link = fixture.anchors[0];
    assert.match(link.download, /^ups-config-\d{4}-\d{2}-\d{2}\.json$/);
    const text = decodeURIComponent(link.href.slice(link.href.indexOf(',') + 1));
    const restored = calculations.normalizeImportData(calculations.parseImportJson(text));
    assert.equal(restored.devices[0].name, 'Router ทดสอบ 🔋');
    assert.equal(restored.devices[0].load, 120);
    assert.match(fixture.alerts[0], /กำลังดาวน์โหลด/);
});

test('post-selection AbortError reports native save failure without a fallback download', async () => {
    for (const stage of ['createWritable', 'write', 'close']) {
        const abort = () => { throw Object.assign(new Error('browser aborted save'), { name: 'AbortError' }); };
        const fixture = exportFixture(async () => ({ createWritable: async () => {
            if (stage === 'createWritable') abort();
            return {
                async write() { if (stage === 'write') abort(); },
                async close() { if (stage === 'close') abort(); },
                async abort() { fixture.events.push('abort'); },
            };
        } }));
        await fixture.run();
        assert.ok(!fixture.events.includes('download'), stage);
        assert.equal(fixture.alerts.length, 1, stage);
        assert.match(fixture.alerts[0], /บันทึกไฟล์ไม่สำเร็จ/, stage);
        assert.ok(!fixture.alerts[0].includes('✅'), stage);
    }
});

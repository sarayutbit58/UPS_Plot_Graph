const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const catalog = require('../catalog.js');
const optimization = require('../optimization.js');

function fakeNode(tag) {
    return {
        tag, children: [], textContent: '', className: '', attrs: {}, listeners: {},
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren(...children) { this.children = children; },
        setAttribute(name, value) { this.attrs[name] = String(value); },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        focus() { this.focused = true; },
    };
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
function textOf(node) { return descendants(node).map(item => item.textContent).join(' '); }

test('Enterprise fixed inventory presents editable counts per selected configuration', () => {
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    assert.match(html, /id="allocationModeAuto"[^>]*checked/);
    assert.match(html, /id="allocationModeFixed"/);
    const start = html.indexOf('function renderFixedInventory()');
    const end = html.indexOf('function allocationModeChanged()', start);
    assert.ok(start >= 0 && end > start, 'fixed inventory renderer exists');
    const selected = catalog.createSelection();
    selected.toggle(11, 'base', 'Model A · base', true);
    selected.toggle(11, 'extra', 'Model A · extra', true);
    const target = fakeNode('div');
    let invalidations = 0;
    const context = vm.createContext({ catalogSelection: selected, UPSCatalog: catalog,
        document: { getElementById(id) { return id === 'fixedInventoryRows' ? target : undefined; },
            createElement: fakeNode }, invalidateOptimizationResults() { invalidations++; } });
    vm.runInContext(html.slice(start, end), context);
    context.renderFixedInventory();
    const inputs = descendants(target).filter(item => item.tag === 'input');
    assert.equal(inputs.length, 2);
    assert.deepEqual(inputs.map(input => input.value), ['1', '1']);
    inputs[0].value = '2';
    inputs[0].listeners.input();
    assert.equal(selected.fixedModels()[0].quantity, 2);
    assert.ok(invalidations > 0, 'editing quantity invalidates stale results');
    inputs[1].value = '0';
    inputs[1].listeners.blur();
    assert.equal(inputs[1].attrs['aria-invalid'], 'true');
    assert.match(textOf(target), /จำนวน.*UPS/);
});

test('fixed inventory retains separate quantities for mixed UPS and battery configurations', () => {
    const selected = catalog.createSelection();
    assert.equal(typeof selected.setQuantity, 'function', 'fixed quantity input is supported');
    assert.equal(typeof selected.fixedModels, 'function', 'fixed selections can be validated');
    selected.toggle(11, 'base', 'A base', true);
    selected.toggle(11, 'extra', 'A extra', true);
    selected.toggle(22, 'base', 'B base', true);
    selected.setQuantity(11, 'base', '2');
    selected.setQuantity(11, 'extra', '3');
    selected.setQuantity(22, 'base', '1');
    assert.deepEqual(selected.fixedModels(), [
        { productId: 11, batteryConfigId: 'base', quantity: 2 },
        { productId: 11, batteryConfigId: 'extra', quantity: 3 },
        { productId: 22, batteryConfigId: 'base', quantity: 1 },
    ]);
    assert.deepEqual(selected.configurations(), [
        { productId: 11, batteryConfigId: 'base' },
        { productId: 11, batteryConfigId: 'extra' },
        { productId: 22, batteryConfigId: 'base' },
    ], 'automatic selection remains pair-only');
    selected.toggle(11, 'base', 'A base', true);
    assert.equal(selected.fixedModels()[0].quantity, 2, 'rerender does not reset quantity');
});

test('fixed inventory rejects missing config, invalid count and backend limits before sending', () => {
    const selected = catalog.createSelection();
    assert.equal(typeof selected.fixedModels, 'function');
    assert.throws(() => selected.fixedModels(), /เลือก.*รุ่น/);
    selected.toggle(11, null, 'Unknown', true);
    assert.throws(() => selected.fixedModels(), /แบตเตอรี่/);
    selected.clear();
    selected.toggle(11, 'base', 'A', true);
    for (const value of ['', '0', '-1', '1.5', '1e2', '1.00000000000000001', '501', true]) {
        selected.setQuantity(11, 'base', value);
        assert.throws(() => selected.fixedModels(), /จำนวน.*UPS/);
    }
    selected.setQuantity(11, 'base', '500');
    selected.toggle(22, 'base', 'B', true);
    assert.throws(() => selected.fixedModels(), /500/);
    selected.clear();
    for (let i = 0; i < 21; i++) selected.toggle(100, `config-${i}`, 'A', true);
    assert.throws(() => selected.fixedModels(), /20/);
});

test('fixed request preserves devices and rejects duplicate product/configuration pairs', () => {
    assert.equal(typeof optimization.buildFixedRequest, 'function');
    const base = optimization.buildRequest([{ name: 'Server', load: 100, unit: 'W', quantity: 2,
        plugs: 2, plugGroups: [{ connector: 'IEC_C14', count: 1 }, { connector: null, count: 1 }] }], 10, 200, 220);
    const models = [{ productId: 11, batteryConfigId: 'base', quantity: 2 },
        { productId: 11, batteryConfigId: 'extra', quantity: 1 }];
    const request = optimization.buildFixedRequest(base, models);
    assert.equal(request.selectedUps.mode, 'fixed');
    assert.deepEqual(request.selectedUps.models, models);
    assert.deepEqual(request.devices, base.devices);
    assert.equal(request.runtimeRequirementMinutes, 10);
    assert.equal('selectedConfigurations' in request, false);
    assert.throws(() => optimization.buildFixedRequest(base, [models[0], models[0]]), /ซ้ำ/);
    assert.throws(() => optimization.buildFixedRequest(base, [{ ...models[0], batteryConfigId: null }]), /แบตเตอรี่/);
    assert.throws(() => optimization.buildFixedRequest(base, [{ ...models[0], quantity: 1.5 }]), /จำนวน.*UPS/);
});

test('fixed allocation uses the same-origin allocation endpoint and sends the fleet', async t => {
    assert.equal(typeof optimization.allocate, 'function');
    const previousFetch = global.fetch;
    t.after(() => { global.fetch = previousFetch; });
    let sent;
    global.fetch = async (url, options) => {
        sent = { url, ...options };
        return { status: 200, ok: true, json: async () => ({ validation: { isValid: true },
            deviceAllocations: [], unassignedDevices: [], upsUtilization: [], outletAllocations: [] }) };
    };
    const request = { selectedUps: { mode: 'fixed', models: [{ productId: 11, batteryConfigId: 'base', quantity: 2 }] },
        runtimeRequirementMinutes: 10, devices: [{ name: 'Server', quantity: 1, watt: 100, va: 125, priority: 50 }] };
    await optimization.allocate(request);
    assert.equal(sent.url, '/api/allocations');
    assert.equal(sent.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(sent.body), request);
});

test('fixed result shows assigned and unassigned devices, runtime and connector warnings safely', t => {
    assert.equal(typeof optimization.renderAllocation, 'function');
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    optimization.renderAllocation(target, {
        runtimeRequirementMinutes: 10,
        validation: { isValid: false, connectorStatus: 'partial' },
        deviceAllocations: [{ deviceIndex: 0, name: '<img src=x onerror=alert(1)>', upsUnitId: 'UPS-01',
            estimatedRuntimeMinutes: 14, runtimeStatus: 'meets_requirement' }],
        unassignedDevices: [{ deviceIndex: 1, name: 'Server B', quantity: 2, reasons: ['outlet_capacity', 'runtime_unknown'] }],
        upsUtilization: [{ upsUnitId: 'UPS-01', productId: 11, batteryConfigId: 'base', manufacturer: 'APC', model: 'UPS A',
            ratings: { watts: 500, va: 600 }, usedLoad: { watts: 250, va: 300 }, outlets: { used: 2, available: 4 },
            estimatedRuntimeMinutes: 14, runtimeStatus: 'meets_requirement' },
        { upsUnitId: 'UPS-02', productId: 11, batteryConfigId: 'extra', manufacturer: 'APC', model: 'UPS A',
            ratings: { watts: 500, va: 600 }, usedLoad: { watts: 0, va: 0 }, outlets: { used: 1, available: 4 },
            estimatedRuntimeMinutes: null, runtimeStatus: 'not_applicable_zero_load' }],
        outletAllocations: [],
    }, [{ productId: 11, batteryConfigId: 'base', label: 'APC · base' },
        { productId: 11, batteryConfigId: 'extra', label: 'APC · extra' }]);
    const text = textOf(target);
    assert.match(text, /จัดสรร/);
    assert.match(text, /Server B/);
    assert.match(text, /2/);
    assert.match(text, /Runtime.*14/);
    assert.match(text, /0 W/);
    assert.match(text, /ชนิดหัวปลั๊กบางส่วน/);
    assert.match(text, /ช่อง.*ไม่พอ|เต้ารับ.*ไม่พอ/);
    assert.match(text, /Runtime.*ไม่ทราบ|ไม่ทราบ.*Runtime/);
    assert.match(text, /<img src=x onerror=alert\(1\)>/);
    assert.equal(descendants(target).some(item => item.tag === 'img'), false);
    assert.doesNotMatch(text, /ราคา\s*0|฿\s*0/);
});

test('fixed result distinguishes all connector verification states', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const descriptions = { verified: /ตรวจชนิดหัวปลั๊กครบ/, partial: /ตรวจชนิดหัวปลั๊กบางส่วน/,
        not_checked: /ยังไม่ได้ตรวจชนิดหัวปลั๊ก/, not_verified: /ยังยืนยันความเข้ากันได้.*ไม่ได้/ };
    for (const [status, expected] of Object.entries(descriptions)) {
        const target = fakeNode('section');
        optimization.renderAllocation(target, { runtimeRequirementMinutes: 10,
            validation: { isValid: true, connectorStatus: status }, deviceAllocations: [],
            unassignedDevices: [], upsUtilization: [], outletAllocations: [] });
        assert.match(textOf(target), expected);
    }
});

test('Enterprise run sends fixed fleet and does not call optimizer in fixed mode', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    const source = html.slice(html.indexOf('async function runScenarioOptimization()'), html.indexOf('// Navigation'));
    const selected = catalog.createSelection();
    assert.equal(typeof selected.setQuantity, 'function');
    selected.toggle(11, 'base', 'A', true);
    selected.toggle(11, 'extra', 'A extra', true);
    selected.setQuantity(11, 'base', '2');
    selected.setQuantity(11, 'extra', '1');
    const elements = { runOptimizationButton: { disabled: false, textContent: '' },
        optimizationStatus: { textContent: '' }, optimizationResults: { replaceChildren() {} },
        backupTimeMin: { value: '10' }, allocationModeFixed: { checked: true } };
    const fields = { '.device-name': 'Server', '.device-load': '100', '.device-unit': 'W',
        '.device-quantity': '1', '.device-plugs': '1' };
    const card = { querySelector(selector) { return { value: fields[selector] }; } };
    let sent;
    let optimizerCalls = 0;
    const context = vm.createContext({ currentMode: 'enterprise', updateTotalLoad() {}, loadInputValid: true,
        calculatedData: { deviceLoad: 100, totalLoad: 100 }, optimizationRequestVersion: 0,
        document: { getElementById(id) { return elements[id]; }, querySelectorAll() { return [card]; } },
        readPlugGroups() { return [{ connector: null, count: '1' }]; }, catalogSelection: selected,
        UPSOptimization: { ...optimization, async allocate(request) {
            sent = request;
            return { validation: { isValid: true, connectorStatus: 'not_checked' },
                deviceAllocations: [{ name: 'Server' }], unassignedDevices: [], upsUtilization: [], outletAllocations: [] };
        }, async optimize() { optimizerCalls++; }, renderAllocation() {} },
    });
    await vm.runInContext(`(${source})`, context)();
    assert.equal(optimizerCalls, 0);
    assert.ok(sent, 'fixed allocation receives a request');
    assert.deepEqual(JSON.parse(JSON.stringify(sent.selectedUps)), { mode: 'fixed', models: [
        { productId: 11, batteryConfigId: 'base', quantity: 2 },
        { productId: 11, batteryConfigId: 'extra', quantity: 1 },
    ] });
    assert.equal('selectedConfigurations' in sent, false);
});

test('switching mode while a request is pending discards its stale result', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    const source = html.slice(html.indexOf('function invalidateOptimizationResults()'), html.indexOf('// Navigation'));
    const selected = catalog.createSelection();
    selected.toggle(11, 'base', 'A', true);
    const elements = { runOptimizationButton: { disabled: false, textContent: '' },
        optimizationStatus: { textContent: '' }, optimizationResults: { replaceChildren() {} },
        backupTimeMin: { value: '10' }, allocationModeFixed: { checked: true } };
    const fields = { '.device-name': 'Server', '.device-load': '100', '.device-unit': 'W',
        '.device-quantity': '1', '.device-plugs': '1' };
    const card = { querySelector(selector) { return { value: fields[selector] }; } };
    let finishFixed, finishAutomatic;
    const fixedResponse = new Promise(resolve => { finishFixed = resolve; });
    const automaticResponse = new Promise(resolve => { finishAutomatic = resolve; });
    const renders = [];
    const context = vm.createContext({ currentMode: 'enterprise', updateTotalLoad() {}, loadInputValid: true,
        calculatedData: { deviceLoad: 100, totalLoad: 100 }, optimizationRequestVersion: 0,
        document: { getElementById(id) { return elements[id]; }, querySelectorAll() { return [card]; } },
        readPlugGroups() { return [{ connector: null, count: '1' }]; }, catalogSelection: selected,
        UPSOptimization: { ...optimization, allocate() { return fixedResponse; }, optimize() { return automaticResponse; },
            renderAllocation() { renders.push('fixed'); }, render() { renders.push('automatic'); } } });
    vm.runInContext(source, context);
    const pendingFixed = context.runScenarioOptimization();
    elements.allocationModeFixed.checked = false;
    context.invalidateOptimizationResults();
    const pendingAutomatic = context.runScenarioOptimization();
    finishFixed({ validation: { isValid: true }, unassignedDevices: [] });
    await pendingFixed;
    assert.equal(elements.runOptimizationButton.disabled, true, 'new request remains pending');
    assert.deepEqual(renders, []);
    finishAutomatic({ validDesignCount: 1, search: { candidateModelCount: 1 } });
    await pendingAutomatic;
    assert.deepEqual(renders, ['automatic']);
    assert.equal(elements.runOptimizationButton.disabled, false);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
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

test('manual selection keeps multiple battery configurations for one product and removes only one pair', () => {
    const selection = catalog.createSelection();
    selection.toggle(11, 'base', 'Model A · base', true);
    selection.toggle(11, 'extra', 'Model A · extra', true);
    selection.toggle(22, null, 'Model B · diagnostic', true);
    assert.deepEqual(selection.configurations(), [
        { productId: 11, batteryConfigId: 'base' },
        { productId: 11, batteryConfigId: 'extra' },
        { productId: 22, batteryConfigId: null },
    ]);
    assert.equal(selection.has(11, 'base'), true);
    selection.toggle(11, 'base', '', false);
    assert.equal(selection.has(11, 'base'), false);
    assert.equal(selection.has(11, 'extra'), true);
    assert.deepEqual(selection.configurations(), [
        { productId: 11, batteryConfigId: 'extra' }, { productId: 22, batteryConfigId: null },
    ]);
    for (let index = 0; index < 98; index++) selection.toggle(100, `config-${index}`, 'Model', true);
    assert.throws(() => selection.toggle(100, 'too-many', 'Too many', true));
    assert.equal(selection.configurations().length, 100);
    selection.clear();
    assert.deepEqual(selection.configurations(), []);
});

test('manual selection rejects malformed product or configuration IDs', () => {
    const selection = catalog.createSelection();
    for (const [productId, configId] of [[0, 'base'], [1, ''], [1, ' x '], [1, 3], [1, 'x\ny']]) {
        assert.throws(() => selection.toggle(productId, configId, 'Bad', true));
    }
});

function fakeNode(tag) {
    return {
        tag, children: [], attrs: {}, listeners: {}, textContent: '', className: '',
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren(...children) { this.children = children; },
        setAttribute(name, value) { this.attrs[name] = String(value); },
        addEventListener(name, listener) { this.listeners[name] = listener; },
        focus() { this.focused = true; },
        querySelectorAll(selector) { return descendants(this).filter(child => child.tag === selector); },
    };
}

function descendants(node) {
    return [node, ...node.children.flatMap(descendants)];
}

test('missing comparison price identifies the Dealer or NDP offer rather than all source prices', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{ productId: 11, manufacturer: 'APC', model: 'Example',
        status: 'unknown', identityVerified: false, ratingsVerified: {}, batteryBackupOutletCount: null,
        checks: {}, expansion: {}, price: null, priceUnavailableReason: 'missing_price',
        canOptimize: false, batteryConfigurations: [] }] }, catalog.createSelection(), () => {}, () => {});
    const text = descendants(target).map(item => item.textContent).join(' ');
    assert.match(text, /ไม่มีราคา Dealer\/NDP ที่ใช้เปรียบเทียบได้/);
});

test('catalog renders selectable battery configurations, unresolved facts, and added-pack price warning', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const selection = catalog.createSelection();
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 11, manufacturer: 'APC', model: 'Example', sku: 'SKU', status: 'unknown',
        identityVerified: false, ratingsVerified: { watts: false, va: true }, batteryBackupOutletCount: null,
        checks: {}, expansion: { supported: true }, price: null, canOptimize: false,
        batteryConfigurations: [
            { id: 'base', label: 'แบตมาตรฐาน', extraPacks: [], runtimeMinutes: 10, source: { url: 'https://www.apc.com/example' } },
            { id: 'extra', label: 'เพิ่มแบต', extraPacks: [{ sku: 'BAT1', quantity: 2 }], runtimeMinutes: null, source: { url: 'javascript:alert(1)' } },
        ],
    }] }, selection, () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    const text = nodes.map(node => node.textContent).join(' ');
    assert.match(text, /รหัสรุ่น.*ไม่ยืนยัน/);
    assert.match(text, /W.*ไม่ยืนยัน/);
    assert.match(text, /ช่อง.*Battery Backup.*ไม่ยืนยัน/);
    assert.match(text, /BAT1.*2/);
    assert.match(text, /ราคา.*แบต.*ไม่ยืนยัน/);
    const links = nodes.filter(node => node.tag === 'a');
    assert.equal(links.length, 1);
    assert.match(links[0].href, /^https:\/\/www\.apc\.com\//);
    const checkboxes = nodes.filter(node => node.tag === 'input' && node.type === 'checkbox');
    assert.equal(checkboxes.length, 2);
    for (const checkbox of checkboxes) {
        checkbox.checked = true;
        checkbox.listeners.change();
    }
    assert.deepEqual(selection.configurations(), [
        { productId: 11, batteryConfigId: 'base' }, { productId: 11, batteryConfigId: 'extra' },
    ]);
});

test('catalog can select an unresolved model with a null configuration for diagnostics', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const selection = catalog.createSelection();
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 12, manufacturer: 'APC', model: 'Unknown', status: 'unknown',
        identityVerified: false, ratingsVerified: { watts: false, va: false },
        batteryBackupOutletCount: null, checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [],
    }] }, selection, () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    assert.match(nodes.map(node => node.textContent).join(' '), /ยังไม่มี.*ชุดแบต/);
    const checkbox = nodes.find(node => node.tag === 'input' && node.type === 'checkbox');
    checkbox.checked = true;
    checkbox.listeners.change();
    assert.deepEqual(selection.configurations(), [{ productId: 12, batteryConfigId: null }]);
});

test('catalog shows verified outlet groups without implying unknown outlets have backup', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 18, manufacturer: 'APC', model: 'Example', status: 'unknown',
        identityVerified: true, ratingsVerified: { watts: true, va: true },
        batteryBackupOutletCount: 3, checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [], outletGroups: [
            { type: 'IEC_C13', power: 'battery', count: 3, source: { url: 'https://www.se.com/spec' } },
            { type: 'IEC_C19', power: 'surge', count: 1, source: { url: 'https://www.se.com/spec' } },
            { type: 'AS', power: 'unknown', count: 2, source: { url: 'https://www.se.com/spec' } },
        ],
    }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const text = descendants(target).map(node => node.textContent).join(' ');
    assert.match(text, /IEC C13.*3.*Battery Backup/);
    assert.match(text, /IEC C19.*1.*กันไฟกระชาก/);
    assert.match(text, /AS.*2.*ยังไม่ทราบ/);
});

test('catalog manufacturer evidence links retain a verified PDF page locator', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 19, manufacturer: 'APC', model: 'Example', status: 'unknown',
        identityVerified: true, ratingsVerified: { watts: true, va: true },
        batteryBackupOutletCount: null, checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [], technicalEvidence: {
            ratingWatts: { url: 'https://www.se.com/spec.pdf', page: 2 },
        },
    }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const link = descendants(target).find(node => node.tag === 'a');
    assert.equal(link.href, 'https://www.se.com/spec.pdf#page=2');
});

test('catalog qualifies a Singapore end-of-sale observation and leaves Thailand unknown', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{ productId: 19, manufacturer: 'APC', model: 'Example',
        manufacturerSku: 'UPS-1', status: 'unknown',
        identityVerified: true, ratingsVerified: {}, batteryBackupOutletCount: null, checks: {}, expansion: {},
        price: null, canOptimize: false, batteryConfigurations: [], lifecycleObservations: [{
            country: 'Singapore', endOfSaleDate: '2025-12-31', observedOn: '2026-10-28', thailandStatus: 'unknown',
            source: { url: 'https://www.se.com/sg/en/product/UPS-1', page: 4 },
        }] }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    const text = nodes.map(node => node.textContent).join(' ');
    assert.match(text, /สิ้นสุดการขายใน Singapore.*2025-12-31/);
    assert.match(text, /สถานะ\/สต็อกไทยยังไม่ทราบ/);
    assert.match(text, /2026-10-28/);
    assert.doesNotMatch(text, /ไทย.*เลิกขาย|ไทย.*ไม่มีสต็อก/);
    assert.equal(nodes.find(node => node.tag === 'a').href, 'https://www.se.com/sg/en/product/UPS-1#page=4');
});

test('catalog displays a Thailand end-of-sale notice only with a Thai regional source', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{ productId: 19, manufacturer: 'APC', model: 'Example',
        manufacturerSku: 'UPS-1', status: 'unknown',
        identityVerified: true, ratingsVerified: {}, batteryBackupOutletCount: null, checks: {}, expansion: {},
        price: null, canOptimize: false, batteryConfigurations: [], lifecycleObservations: [{
            country: 'Thailand', endOfSaleDate: '2026-06-18', observedOn: '2026-10-28', thailandStatus: 'unknown',
            source: { url: 'https://www.se.com/th/en/product/ups-1' },
        }, {
            country: 'Thailand', endOfSaleDate: '2026-05-01', observedOn: '2026-10-28', thailandStatus: 'unknown',
            source: { url: 'https://www.se.com/th/en/product/OTHER-999' },
        }] }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    const text = nodes.map(node => node.textContent).join(' ');
    assert.match(text, /สิ้นสุดการขายในประเทศไทย.*2026-06-18/);
    assert.match(text, /สต็อก.*ตรวจสอบ/);
    assert.doesNotMatch(text, /ไทยยังไม่ทราบ/);
    assert.doesNotMatch(text, /2026-05-01/);
    assert.equal(nodes.find(node => node.tag === 'a').href, 'https://www.se.com/th/en/product/ups-1');
});

test('catalog hides lifecycle notices when the product identity is unverified', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{ productId: 19, manufacturer: 'APC', model: 'Example',
        manufacturerSku: 'UPS-1', identityVerified: false, status: 'unknown', ratingsVerified: {},
        batteryBackupOutletCount: null, checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [], lifecycleObservations: [{
            country: 'Thailand', endOfSaleDate: '2026-06-18', observedOn: '2026-10-28', thailandStatus: 'unknown',
            source: { url: 'https://www.se.com/th/en/product/UPS-1' },
        }] }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    assert.doesNotMatch(descendants(target).map(node => node.textContent).join(' '), /สิ้นสุดการขาย/);
});

test('catalog discards malformed lifecycle observations rather than asserting availability', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    const valid = { country: 'Singapore', endOfSaleDate: '2025-12-31', observedOn: '2026-10-28',
        thailandStatus: 'unknown', source: { url: 'https://www.se.com/spec.pdf', page: 4 } };
    const observations = [null, {}, { ...valid, country: '' }, { ...valid, country: '<img src=x>' },
        { ...valid, country: 'Thailand' }, { ...valid, country: 'Thailand.' },
        { ...valid, country: 'Kingdom of Thailand', source: { url: 'https://www.se.com/sg/en/product/UPS-1' } },
        { ...valid, endOfSaleDate: '2026-02-30' },
        { ...valid, observedOn: 'unknown' }, { ...valid, observedOn: '2099-01-01' },
        { ...valid, thailandStatus: 'end_of_sale' },
        { ...valid, source: { url: 'javascript:alert(1)' } },
        { ...valid, source: { url: 'https://www.se.com/spec.pdf', page: 0 } }];
    catalog.render(target, { models: [{ productId: 19, manufacturer: 'APC', model: 'Example', status: 'unknown',
        identityVerified: true, ratingsVerified: {}, batteryBackupOutletCount: null, checks: {}, expansion: {},
        price: null, canOptimize: false, batteryConfigurations: [], lifecycleObservations: observations }] },
    catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    assert.doesNotMatch(nodes.map(node => node.textContent).join(' '), /สิ้นสุดการขายใน/);
    assert.equal(nodes.some(node => node.tag === 'img'), false);
    assert.equal(nodes.some(node => node.tag === 'a'), false);
});

test('catalog explains why a merged bundle price is unavailable', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 20, manufacturer: 'APC', model: 'Example', status: 'unknown',
        identityVerified: false, ratingsVerified: { watts: true, va: true },
        batteryBackupOutletCount: null, checks: {}, expansion: {}, price: null, canOptimize: false,
        priceUnavailableReason: 'bundle_identity_unverified', batteryConfigurations: [],
    }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    assert.match(descendants(target).map(node => node.textContent).join(' '), /ชุดสินค้า.*ราคาต้นทาง/);
});

test('catalog shows only a safe opaque crosswalk ID and manufacturer link', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 20, manufacturer: 'Eaton', model: 'Example', status: 'unknown',
        identityVerified: true, ratingsVerified: { watts: true, va: true },
        batteryBackupOutletCount: null, checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [], technicalEvidence: { identityCrosswalk: {
            sourceId: 'a'.repeat(24), manufacturerUrl: 'https://www.eaton.com/crosswalk.pdf',
            path: 'C:/private/sensitive.xlsx',
        } },
    }] }, catalog.createSelection(), () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    const text = nodes.map(node => node.textContent).join(' ');
    assert.match(text, /อ้างอิง/);
    assert.match(text, /a{24}/);
    assert.doesNotMatch(text, /private|sensitive/);
    assert.equal(nodes.find(node => node.tag === 'a').href, 'https://www.eaton.com/crosswalk.pdf');
});

test('catalog treats malformed backup counts and runtime as unknown and rejects duplicate configuration IDs', t => {
    const previous = global.document;
    global.document = { createElement: fakeNode };
    t.after(() => { global.document = previous; });
    const selection = catalog.createSelection();
    const target = fakeNode('section');
    catalog.render(target, { models: [{
        productId: 13, manufacturer: 'APC', model: 'Unknown', status: 'unknown', identityVerified: true,
        ratingsVerified: { watts: true, va: true }, batteryBackupOutletCount: 'invalid',
        checks: {}, expansion: {}, price: null, canOptimize: false,
        batteryConfigurations: [
            { id: 'duplicate', label: 'First', extraPacks: [], runtimeMinutes: 10 },
            { id: 'duplicate', label: 'Second', extraPacks: [], runtimeMinutes: 20 },
            { id: 'valid', label: 'Valid', extraPacks: [], runtimeMinutes: NaN },
        ],
    }] }, selection, () => {}, error => { throw new Error(error); });
    const nodes = descendants(target);
    const text = nodes.map(node => node.textContent).join(' ');
    assert.match(text, /จำนวนช่อง Battery Backup ยังไม่ยืนยัน/);
    assert.doesNotMatch(text, /ช่อง Battery Backup ที่ยืนยัน: ไม่ทราบ/);
    assert.match(text, /Runtime ที่โหลดปัจจุบัน: ไม่ทราบ/);
    assert.doesNotMatch(text, /ไม่ทราบ นาที/);
    assert.match(text, /ชุดแบตเตอรี่บางรายการไม่ถูกต้อง/);
    assert.equal(nodes.filter(node => node.tag === 'input' && node.type === 'checkbox').length, 1);
});

test('optimizer request sends selected product/configuration pairs and omits filter when none are selected', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    const source = html.slice(html.indexOf('async function runScenarioOptimization()'), html.indexOf('// Navigation'));
    const optimization = require('../optimization.js');
    for (const withSelection of [true, false]) {
        const selection = catalog.createSelection();
        if (withSelection) {
            selection.toggle(11, 'base', 'Base', true);
            selection.toggle(11, 'extra', 'Extra', true);
        }
        const elements = {
            runOptimizationButton: { disabled: false, textContent: '' },
            optimizationStatus: { textContent: '' }, optimizationResults: { replaceChildren() {} },
            backupTimeMin: { value: '10' },
        };
        const fields = { '.device-name': 'Server', '.device-load': '100', '.device-unit': 'W',
            '.device-quantity': '1', '.device-plugs': '1' };
        const card = { querySelector(selector) { return { value: fields[selector] }; } };
        let sent;
        const context = vm.createContext({
            currentMode: 'enterprise', updateTotalLoad() {}, loadInputValid: true,
            calculatedData: { deviceLoad: 100, totalLoad: 100 }, optimizationRequestVersion: 0,
            document: { getElementById(id) { return elements[id]; }, querySelectorAll() { return [card]; } },
            readPlugGroups() { return [{ connector: null, count: '1' }]; }, catalogSelection: selection,
            UPSOptimization: { ...optimization, async optimize(request) {
                sent = request;
                return { validDesignCount: 0, search: { candidateModelCount: 0 } };
            }, render() {} },
        });
        await vm.runInContext(`(${source})`, context)();
        assert.ok(sent, 'optimizer receives a request');
        if (withSelection) {
            assert.ok(Array.isArray(sent.selectedConfigurations), 'selection is sent as product/configuration pairs');
            assert.deepEqual(JSON.parse(JSON.stringify(sent.selectedConfigurations)), [
                { productId: 11, batteryConfigId: 'base' }, { productId: 11, batteryConfigId: 'extra' },
            ]);
        }
        else assert.equal('selectedConfigurations' in sent, false);
        assert.equal('selectedProductIds' in sent, false);
    }
});

test('selected-list removal removes just one configuration and updates the catalog', () => {
    const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
    const source = html.slice(html.indexOf('function catalogSelectionChanged('), html.indexOf('function clearCatalogSelection()'));
    const selection = catalog.createSelection();
    selection.toggle(11, 'base', 'APC · base', true);
    selection.toggle(11, 'extra', 'APC · extra', true);
    const elements = { catalogSelectionSummary: fakeNode('p'), optimizerPoolStatus: fakeNode('p'),
        catalogSelectedModels: fakeNode('ul'), catalogResults: fakeNode('section'), catalogStatus: fakeNode('p') };
    let renders = 0;
    const context = vm.createContext({
        catalogSelection: selection, catalogLastResult: { models: [] },
        document: { getElementById(id) { return elements[id]; }, createElement: fakeNode },
        UPSCatalog: { render() { renders++; } }, invalidateOptimizationResults() {},
    });
    vm.runInContext(source, context);
    context.catalogSelectionChanged(false);
    assert.equal(renders, 0, 'checking a configuration does not rerender the focused checkbox');
    const remove = elements.catalogSelectedModels.children[0].children[0];
    remove.listeners.click();
    assert.deepEqual(selection.configurations(), [{ productId: 11, batteryConfigId: 'extra' }]);
    assert.equal(renders, 1);
    assert.equal(elements.catalogSelectedModels.children[0].children[0].focused, true);
});

test('disabled filters remain visibly disabled and unknown runtime discloses curve bounds', () => {
    const text = catalog.describeCheck('runtime', { status: 'unknown', enabled: false, required: 10, available: null },
        { runtimeLoadRangeWatts: { minimum: 300, maximum: 600 } });
    assert.match(text, /ไม่ได้ใช้กรอง/);
    assert.match(text, /300–600 W/);
    assert.doesNotMatch(text, /: ผ่าน/);
});

test('outlet checks are labeled as battery-backed rather than generic receptacles', () => {
    const text = catalog.describeCheck('outlets', { status: 'pass', enabled: true, required: 2, available: 4 }, {});
    assert.match(text, /Battery Backup/);
    assert.match(text, /มี 4 \/ ต้องการ 2/);
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

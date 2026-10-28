const test = require('node:test');
const assert = require('node:assert/strict');
const optimization = require('../optimization.js');

function node(tag) {
    return { tag, children: [], textContent: '', className: '',
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren(...children) { this.children = children; } };
}
function textOf(item) { return [item.textContent, ...item.children.map(textOf)].join(' '); }

test('optimizer shows the selected battery setup and unchecked connector warning', t => {
    const previous = global.document;
    global.document = { createElement: node };
    t.after(() => { global.document = previous; });
    const target = node('section');
    optimization.render(target, { notes: [], scenarios: [], topAlternatives: [], bestSolution: {
        models: [{ manufacturer: 'APC', model: 'UPS-1', batteryConfiguration: 'แบตเพิ่ม 1 ชุด', quantity: 1 }],
        unitCount: 1, minimumEstimatedRuntimeMinutes: 20, runtimeReserveMinutes: 10,
        expansion: { maxBatteryPacks: 1 }, units: [], engineeringJustification: [],
        validation: { connectorStatus: 'not_checked' },
    } });
    const text = textOf(target);
    assert.match(text, /แบตเพิ่ม 1 ชุด/);
    assert.match(text, /ยังไม่ได้ตรวจชนิดหัว/);
});

test('fixed allocation clearly says when bounded search did not finish', t => {
    const previous = global.document;
    global.document = { createElement: node };
    t.after(() => { global.document = previous; });
    const target = node('section');
    optimization.renderAllocation(target, { runtimeRequirementMinutes: 10,
        deviceAllocations: [], unassignedDevices: [], upsUtilization: [], outletAllocations: [],
        validation: { isValid: false, searchComplete: false, searchReason: 'fixed_search_node_limit' },
    });
    assert.match(textOf(target), /ค้นหา.*ไม่ครบ/);
});

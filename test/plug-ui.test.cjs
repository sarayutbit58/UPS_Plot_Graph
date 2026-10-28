const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const calculations = require('../calculations.js');

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const source = html.slice(html.indexOf('function plugGroupsMarkup('), html.indexOf('function addDevice()'));
const context = vm.createContext({ UPScalculations: calculations, updateTotalLoad() {} });
const ui = vm.runInContext(`${source}; ({ plugGroupsMarkup, readPlugGroups, initializePlugGroups,
    handleTotalPlugsInput, handlePlugGroupInput })`, context);

function cardFixture(total = '1') {
    const feedback = { textContent: '', hidden: true };
    feedback.classList = { toggle(name, hidden) { assert.equal(name, 'hidden'); feedback.hidden = hidden; } };
    const totalInput = { value: total };
    const fields = ['IEC_C14', 'IEC_C20', 'NEMA_5_15P', ''].map(connector => ({
        value: connector ? '0' : total, dataset: { connector, previous: connector ? '0' : total },
    }));
    const card = {
        querySelector(selector) {
            if (selector === '.device-plugs') return totalInput;
            if (selector === '.device-plug-error') return feedback;
            throw new Error(`Unexpected selector ${selector}`);
        },
        querySelectorAll(selector) {
            assert.equal(selector, '.device-plug-group');
            return fields;
        },
    };
    totalInput.closest = () => card;
    fields.forEach(input => { input.closest = () => card; });
    return { card, fields, totalInput, feedback };
}

test('choosing a type replaces an unspecified plug and preserves physical quantity', () => {
    const { card, fields, totalInput, feedback } = cardFixture();
    ui.initializePlugGroups(card);
    fields[0].value = '1';
    ui.handlePlugGroupInput(fields[0]);
    assert.equal(totalInput.value, '1');
    assert.equal(fields[3].value, '0');
    assert.equal(feedback.hidden, true);
    assert.deepEqual(JSON.parse(JSON.stringify(ui.readPlugGroups(card))), [{ connector: 'IEC_C14', count: '1' }]);
    fields[1].value = '1';
    ui.handlePlugGroupInput(fields[1]);
    assert.equal(totalInput.value, '2');
    assert.deepEqual(JSON.parse(JSON.stringify(ui.readPlugGroups(card))), [
        { connector: 'IEC_C14', count: '1' }, { connector: 'IEC_C20', count: '1' },
    ]);
});

test('restores multiple groups and flags a total smaller than the specified connectors', () => {
    const { card, fields, totalInput, feedback } = cardFixture('3');
    ui.initializePlugGroups(card, [
        { connector: 'IEC_C14', count: 2 }, { connector: 'NEMA_5_15P', count: 1 },
    ]);
    assert.deepEqual(fields.map(input => input.value), ['2', '0', '1', '0']);
    totalInput.value = '2';
    ui.handleTotalPlugsInput(totalInput);
    assert.equal(feedback.hidden, false);
    assert.match(feedback.textContent, /เท่ากับจำนวนปลั๊ก/);
});

test('does not discard an invalid tiny connector count as zero', () => {
    const { card, fields, feedback } = cardFixture();
    ui.initializePlugGroups(card);
    fields[0].value = '1e-324';
    ui.handlePlugGroupInput(fields[0]);
    assert.equal(feedback.hidden, false);
    assert.match(feedback.textContent, /จำนวนปลั๊ก/);
});

test('optional connector controls have labels and explain cable versus UPS socket', () => {
    const markup = ui.plugGroupsMarkup(7);
    assert.match(markup, /<details/);
    assert.match(markup, /ไม่บังคับ|ถ้าต้องการ/);
    assert.match(markup, /IEC C14 → ช่อง UPS C13/);
    assert.match(markup, /ไม่ระบุชนิด/);
    assert.match(markup, /<label for="device-7-plug-group-0"/);
    assert.match(markup, /aria-describedby="device-7-plug-help device-7-plug-error"/);
});

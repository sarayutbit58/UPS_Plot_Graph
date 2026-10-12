(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.UPScalculations = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    function finiteNumber(value) {
        if (value === '' || value === null || value === undefined) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function round(value, places = 2) {
        const factor = 10 ** places;
        return Math.round((value + Number.EPSILON) * factor) / factor;
    }

    function calculateLoadBreakdown(inputDevices, forecastValue, forecastUnit) {
        const devices = [];
        let deviceLoad = 0;
        let totalPlugs = 0;
        (Array.isArray(inputDevices) ? inputDevices : []).forEach(device => {
            const load = finiteNumber(device && device.load);
            if (load === null || load <= 0) return;
            const unit = device.unit === 'VA' ? 'VA' : 'W';
            const watts = unit === 'VA' ? load * 0.8 : load;
            const plugsValue = finiteNumber(device.plugs);
            const plugs = plugsValue !== null && Number.isInteger(plugsValue) && plugsValue > 0 ? plugsValue : 1;
            const normalized = { name: String(device.name ?? ''), load, unit, watts: round(watts), plugs };
            devices.push(normalized);
            deviceLoad += watts;
            totalPlugs += plugs;
        });
        const rawForecast = finiteNumber(forecastValue);
        const value = rawForecast !== null && rawForecast > 0 ? rawForecast : 0;
        const unit = forecastUnit === 'W' ? 'W' : '%';
        const forecastWatt = value > 0 ? (unit === '%' ? deviceLoad * value / 100 : value) : 0;
        return {
            devices,
            deviceLoad: round(deviceLoad),
            forecastWatt: round(forecastWatt),
            totalLoad: round(deviceLoad + forecastWatt),
            totalPlugs,
        };
    }

    function calculateBatteryAh(powerW, minutes, voltage, efficiency) {
        const power = finiteNumber(powerW), time = finiteNumber(minutes);
        const volts = finiteNumber(voltage), percent = finiteNumber(efficiency);
        if (power === null || power <= 0 || time === null || time <= 0 || volts === null || volts <= 0 || percent === null || percent <= 0 || percent > 100) return null;
        return round((power * (time / 60)) / (volts * (percent / 100)));
    }

    function calculateRuntimeMinutes(ampHours, voltage, efficiency, powerW) {
        const ah = finiteNumber(ampHours), volts = finiteNumber(voltage);
        const percent = finiteNumber(efficiency), power = finiteNumber(powerW);
        if (ah === null || ah < 0 || volts === null || volts <= 0 || percent === null || percent <= 0 || percent > 100 || power === null || power <= 0) return null;
        return Math.round((ah * volts * (percent / 100) / power) * 60);
    }

    function buildRuntimeCurve(ampHours, voltage, efficiency, currentLoadW) {
        const current = finiteNumber(currentLoadW);
        if (current === null || current <= 0) return [];
        const loads = [...new Set([50, 100, 150, 200, 250, 300, current])].sort((a, b) => a - b);
        return loads.map(loadW => ({
            loadW,
            runtimeMinutes: calculateRuntimeMinutes(ampHours, voltage, efficiency, loadW),
            currentLoad: loadW === current,
        }));
    }

    function recommendUpsVA(loadW) {
        const load = finiteNumber(loadW);
        return load === null || load <= 0 ? 0 : Math.ceil(load * 1.3 / 100) * 100;
    }

    function planOutlets(totalPlugs) {
        const count = finiteNumber(totalPlugs);
        if (count === null || count <= 0) return { requiredUpsCount: 0, outletsPerUps: 0 };
        const configs = [4, 6, 8, 10, 12];
        const outletsPerUps = configs.find(outlets => outlets >= count) || 0;
        return { requiredUpsCount: outletsPerUps ? 1 : Math.ceil(count / 12), outletsPerUps };
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function normalizeImportData(data) {
        if (!data || typeof data !== 'object' || !Array.isArray(data.devices) || !data.calculatedData || typeof data.calculatedData !== 'object') {
            throw new Error('รูปแบบไฟล์ไม่ถูกต้อง');
        }
        const devices = data.devices.map(device => {
            if (!device || typeof device !== 'object') throw new Error('ข้อมูลอุปกรณ์ไม่ถูกต้อง');
            const load = finiteNumber(device.load);
            const plugs = device.plugs === undefined ? 1 : finiteNumber(device.plugs);
            if (load === null || load <= 0 || plugs === null || !Number.isInteger(plugs) || plugs < 1 || !['W', 'VA'].includes(device.unit ?? 'W')) {
                throw new Error('ข้อมูลโหลดหรือจำนวนปลั๊กไม่ถูกต้อง');
            }
            return { name: String(device.name ?? ''), load, unit: device.unit ?? 'W', plugs };
        });
        const sourceSettings = data.settings || {};
        const settings = {
            backupTime: finiteNumber(sourceSettings.backupTime ?? data.calculatedData.backupTime ?? 60),
            voltage: finiteNumber(sourceSettings.voltage ?? data.calculatedData.voltage ?? 12),
            efficiency: finiteNumber(sourceSettings.efficiency ?? data.calculatedData.efficiency ?? 85),
            runtimeBatteryAh: sourceSettings.runtimeBatteryAh === undefined || sourceSettings.runtimeBatteryAh === null
                ? null : finiteNumber(sourceSettings.runtimeBatteryAh),
        };
        if (settings.backupTime === null || settings.backupTime <= 0 || settings.voltage === null || settings.voltage <= 0 || settings.efficiency === null || settings.efficiency <= 0 || settings.efficiency > 100 || (settings.runtimeBatteryAh !== null && settings.runtimeBatteryAh < 0)) {
            throw new Error('ค่าตั้งค่าแบตเตอรี่ไม่ถูกต้อง');
        }
        const forecastSource = data.forecast && typeof data.forecast === 'object' ? data.forecast : null;
        const legacyForecast = data.forecastWatt ?? data.calculatedData.forecastWatt ?? 0;
        const forecastValue = finiteNumber(forecastSource ? (forecastSource.value ?? 0) : legacyForecast);
        const forecastUnit = forecastSource ? (forecastSource.unit ?? '%') : (legacyForecast ? 'W' : '%');
        if (forecastValue === null || forecastValue < 0 || !['%', 'W'].includes(forecastUnit)) throw new Error('ค่า Forecast ไม่ถูกต้อง');
        return { devices, settings, forecast: { value: forecastValue, unit: forecastUnit } };
    }

    function buildExportData({ devices, calculatedData, settings, exportDate = new Date() }) {
        return {
            version: '1.3', appName: 'UPS Picker', exportDate: exportDate.toISOString(),
            devices: devices.map(device => ({ ...device })),
            calculatedData: { ...calculatedData },
            forecast: { value: calculatedData.forecastValue, unit: calculatedData.forecastUnit, watt: calculatedData.forecastWatt },
            settings: { ...settings },
        };
    }

    return { calculateLoadBreakdown, calculateBatteryAh, calculateRuntimeMinutes, buildRuntimeCurve, recommendUpsVA, planOutlets, escapeHtml, normalizeImportData, buildExportData };
}));

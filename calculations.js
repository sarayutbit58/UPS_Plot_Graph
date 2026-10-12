(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.UPScalculations = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const DEFAULT_POWER_FACTOR = 0.8;

    function finiteNumber(value) {
        if (value === null || value === undefined || typeof value === 'boolean') return null;
        if (typeof value !== 'number' && typeof value !== 'string') return null;
        if (typeof value === 'string' && value.trim() === '') return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function decimalFraction(value) {
        const match = /^(-?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(value.toString());
        if (!match) return null;
        const fraction = match[3] || '';
        const exponent = Number(match[4] || 0);
        let numerator = BigInt(`${match[2]}${fraction}`);
        let denominator = 1n;
        const scale = fraction.length - exponent;
        if (scale > 0) denominator = 10n ** BigInt(scale);
        else if (scale < 0) numerator *= 10n ** BigInt(-scale);
        if (match[1] === '-') numerator = -numerator;
        return { numerator, denominator };
    }

    function scaledIntegerText(value, places) {
        const negative = value < 0n;
        const digits = (negative ? -value : value).toString().padStart(places + 1, '0');
        const whole = places ? digits.slice(0, -places) : digits;
        const fraction = places ? `.${digits.slice(-places)}` : '';
        return `${negative ? '-' : ''}${whole}${fraction}`;
    }

    function roundFractionToScaledInteger(numerator, denominator, places) {
        if (denominator <= 0n) return null;
        const scaledNumerator = numerator * (10n ** BigInt(places));
        const negative = scaledNumerator < 0n;
        const magnitude = negative ? -scaledNumerator : scaledNumerator;
        const rounded = (magnitude * 2n + denominator) / (denominator * 2n);
        return negative ? -rounded : rounded;
    }

    function fromScaledInteger(value, places) {
        const text = scaledIntegerText(value, places);
        const number = Number(text);
        if (!Number.isFinite(number) || Math.abs(number) >= 1e21 || number.toFixed(places) !== text) return null;
        return number;
    }

    function calculateLoadBreakdown(inputDevices, forecastValue, forecastUnit) {
        if (!Array.isArray(inputDevices)) {
            return { devices: [], deviceLoad: 0, forecastWatt: 0, totalLoad: 0, totalPlugs: 0, precisionInvalidTarget: null, valid: false };
        }
        const devices = [];
        let deviceLoad = 0;
        let deviceLoadMicroWatts = 0n;
        let totalPlugs = 0;
        let inputsValid = true;
        let precisionInvalidTarget = null;
        inputDevices.forEach((device, index) => {
            const load = finiteNumber(device && device.load);
            if (load === null) {
                const rawLoad = device && device.load;
                if (rawLoad !== undefined && rawLoad !== null && !(typeof rawLoad === 'string' && rawLoad.trim() === '')) inputsValid = false;
                return;
            }
            if (load < 0) {
                inputsValid = false;
                return;
            }
            const unit = device.unit === undefined || device.unit === null || device.unit === '' ? 'W' : device.unit;
            if (!['W', 'VA'].includes(unit)) {
                inputsValid = false;
                return;
            }
            const loadFraction = decimalFraction(load);
            const wattsNumerator = unit === 'VA' ? loadFraction.numerator * 4n : loadFraction.numerator;
            const wattsDenominator = unit === 'VA' ? loadFraction.denominator * 5n : loadFraction.denominator;
            const plugsValue = finiteNumber(device.plugs);
            const plugs = device.plugs === undefined || device.plugs === null || device.plugs === '' ? 1 : plugsValue;
            if (plugs === null || !Number.isSafeInteger(plugs) || plugs < 1 || !Number.isSafeInteger(totalPlugs + plugs)) {
                inputsValid = false;
                return;
            }
            const rowMicroWatts = roundFractionToScaledInteger(wattsNumerator, wattsDenominator, 6);
            const roundedWatts = rowMicroWatts === null ? null : fromScaledInteger(rowMicroWatts, 6);
            if (roundedWatts === null) {
                inputsValid = false;
                precisionInvalidTarget ??= { type: 'device-load', index };
                return;
            }
            if (load > 0 && roundedWatts === 0) {
                inputsValid = false;
                precisionInvalidTarget ??= { type: 'device-load', index };
                return;
            }
            const nextDeviceLoadMicroWatts = rowMicroWatts === null ? null : deviceLoadMicroWatts + rowMicroWatts;
            const nextDeviceLoad = nextDeviceLoadMicroWatts === null ? null : fromScaledInteger(nextDeviceLoadMicroWatts, 6);
            if (nextDeviceLoad === null) {
                inputsValid = false;
                precisionInvalidTarget ??= { type: 'device-load', index };
                return;
            }
            const normalized = { name: String(device.name ?? ''), load, unit, watts: roundedWatts, plugs };
            devices.push(normalized);
            deviceLoadMicroWatts = nextDeviceLoadMicroWatts;
            deviceLoad = nextDeviceLoad;
            totalPlugs += plugs;
        });
        const rawForecast = finiteNumber(forecastValue);
        const forecastProvided = forecastValue !== undefined && forecastValue !== null && !(typeof forecastValue === 'string' && forecastValue.trim() === '');
        if ((forecastProvided && rawForecast === null) || rawForecast < 0) inputsValid = false;
        const value = rawForecast !== null && rawForecast > 0 ? rawForecast : 0;
        const resolvedForecastUnit = forecastUnit === undefined || forecastUnit === null || forecastUnit === '' ? '%' : forecastUnit;
        if (!['%', 'W'].includes(resolvedForecastUnit)) inputsValid = false;
        const unit = resolvedForecastUnit === 'W' ? 'W' : '%';
        const forecastFraction = decimalFraction(value);
        const forecastNumerator = unit === '%' ? deviceLoadMicroWatts * forecastFraction.numerator : forecastFraction.numerator;
        const forecastDenominator = unit === '%'
            ? 1_000_000n * forecastFraction.denominator * 100n
            : forecastFraction.denominator;
        const forecastMicroWatts = roundFractionToScaledInteger(forecastNumerator, forecastDenominator, 6);
        const roundedForecastWatt = forecastMicroWatts === null ? null : fromScaledInteger(forecastMicroWatts, 6);
        const forecastPositive = value > 0 && (unit === 'W' || deviceLoad > 0);
        if (roundedForecastWatt === null) {
            inputsValid = false;
            if (forecastPositive) precisionInvalidTarget ??= { type: 'forecast' };
        }
        if (forecastPositive && roundedForecastWatt === 0) {
            inputsValid = false;
            precisionInvalidTarget ??= { type: 'forecast' };
        }
        const roundedTotalLoad = forecastMicroWatts === null
            ? null : fromScaledInteger(deviceLoadMicroWatts + forecastMicroWatts, 6);
        if (roundedTotalLoad === null) {
            inputsValid = false;
            precisionInvalidTarget ??= { type: 'forecast' };
        }
        const roundedDeviceLoad = deviceLoad;
        const positiveComponentRoundedAway = (deviceLoad > 0 && roundedDeviceLoad === 0)
            || (forecastPositive && roundedForecastWatt === 0);
        const valid = inputsValid && !positiveComponentRoundedAway
            && roundedDeviceLoad !== null && roundedForecastWatt !== null && roundedTotalLoad !== null;
        return {
            devices,
            deviceLoad: valid ? roundedDeviceLoad : 0,
            forecastWatt: valid ? roundedForecastWatt : 0,
            totalLoad: valid ? roundedTotalLoad : 0,
            totalPlugs,
            precisionInvalidTarget,
            valid,
        };
    }

    function calculateBatteryAh(powerW, minutes, voltage, efficiency) {
        const power = finiteNumber(powerW), time = finiteNumber(minutes);
        const volts = finiteNumber(voltage), percent = finiteNumber(efficiency);
        if (power === null || power <= 0 || time === null || time <= 0 || volts === null || volts <= 0 || percent === null || percent < 50 || percent > 100) return null;
        const p = decimalFraction(power), t = decimalFraction(time);
        const v = decimalFraction(volts), e = decimalFraction(percent);
        const numerator = p.numerator * t.numerator * 100n * v.denominator * e.denominator;
        const denominator = p.denominator * t.denominator * 60n * v.numerator * e.numerator;
        const scaledAh = (numerator * 1_000_000n + denominator - 1n) / denominator;
        return fromScaledInteger(scaledAh > 0n ? scaledAh : 1n, 6);
    }

    function calculateRuntimeMinutes(ampHours, voltage, efficiency, powerW) {
        const ah = finiteNumber(ampHours), volts = finiteNumber(voltage);
        const percent = finiteNumber(efficiency), power = finiteNumber(powerW);
        if (ah === null || ah < 0 || volts === null || volts <= 0 || percent === null || percent < 50 || percent > 100 || power === null || power <= 0) return null;
        const a = decimalFraction(ah), v = decimalFraction(volts);
        const e = decimalFraction(percent), p = decimalFraction(power);
        const numerator = a.numerator * v.numerator * e.numerator * 60n * p.denominator;
        const denominator = a.denominator * v.denominator * e.denominator * 100n * p.numerator;
        const hundredthsNumerator = numerator * 100n;
        const roundedHundredths = (hundredthsNumerator * 2n + denominator) / (denominator * 2n);
        if (ah > 0 && roundedHundredths === 0n) {
            let exactMinutes = (ah / power) * volts * (percent / 100) * 60;
            if (!Number.isFinite(exactMinutes) || exactMinutes === 0) {
                exactMinutes = Math.exp(Math.log(ah) - Math.log(power) + Math.log(volts) + Math.log(percent / 100) + Math.log(60));
            }
            return Number.isFinite(exactMinutes) && exactMinutes > 0 ? exactMinutes : Number.MIN_VALUE;
        }
        return fromScaledInteger(roundedHundredths, 2);
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
        if (load === null || load <= 0) return 0;
        const watts = decimalFraction(load);
        const powerFactor = decimalFraction(DEFAULT_POWER_FACTOR);
        const headroom = decimalFraction(1.3);
        const numerator = watts.numerator * powerFactor.denominator * headroom.numerator;
        const denominator = watts.denominator * powerFactor.numerator * headroom.denominator * 100n;
        const vaUnits = (numerator + denominator - 1n) / denominator;
        const recommendationUnits = vaUnits * 100n;
        const recommendation = Number(recommendationUnits);
        if (!Number.isFinite(recommendation) || BigInt(recommendation) !== recommendationUnits) return null;
        return recommendation;
    }

    function planOutlets(totalPlugs) {
        const count = finiteNumber(totalPlugs);
        if (count === null || !Number.isSafeInteger(count) || count <= 0) return { requiredUpsCount: 0, outletsPerUps: 0 };
        const configs = [4, 6, 8, 10, 12];
        const outletsPerUps = configs.find(outlets => outlets >= count) || 0;
        return { requiredUpsCount: outletsPerUps ? 1 : Math.ceil(count / 12), outletsPerUps };
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function invalidImport(message) {
        const error = new Error(message);
        error.name = 'InvalidImportDataError';
        return error;
    }

    function normalizeImportData(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(data.devices) || !data.calculatedData || typeof data.calculatedData !== 'object' || Array.isArray(data.calculatedData)) {
            throw invalidImport('รูปแบบไฟล์ไม่ถูกต้อง');
        }
        const mode = data.mode === undefined || data.mode === null ? null : data.mode;
        if (mode !== null && !['basic', 'enterprise'].includes(mode)) throw invalidImport('โหมดการใช้งานไม่ถูกต้อง');
        const devices = data.devices.map(device => {
            if (!device || typeof device !== 'object' || Array.isArray(device)) throw invalidImport('ข้อมูลอุปกรณ์ไม่ถูกต้อง');
            const load = finiteNumber(device.load);
            const plugs = device.plugs === undefined ? 1 : finiteNumber(device.plugs);
            if (load === null || load < 0 || plugs === null || !Number.isSafeInteger(plugs) || plugs < 1 || !['W', 'VA'].includes(device.unit ?? 'W') || (device.name !== undefined && typeof device.name !== 'string')) {
                throw invalidImport('ข้อมูลโหลดหรือจำนวนปลั๊กไม่ถูกต้อง');
            }
            return { name: String(device.name ?? ''), load, unit: device.unit ?? 'W', plugs };
        });
        const sourceSettings = data.settings === undefined || data.settings === null ? {} : data.settings;
        if (typeof sourceSettings !== 'object' || Array.isArray(sourceSettings)) throw invalidImport('ค่าตั้งค่าแบตเตอรี่ไม่ถูกต้อง');
        const runtimeBatteryValue = sourceSettings.runtimeBatteryAh;
        const runtimeBatteryAh = runtimeBatteryValue === undefined || runtimeBatteryValue === null
            ? null : finiteNumber(runtimeBatteryValue);
        const settings = {
            backupTime: finiteNumber(sourceSettings.backupTime ?? data.calculatedData.backupTime ?? 60),
            voltage: finiteNumber(sourceSettings.voltage ?? data.calculatedData.voltage ?? 12),
            efficiency: finiteNumber(sourceSettings.efficiency ?? data.calculatedData.efficiency ?? 85),
            runtimeBatteryAh,
        };
        if (settings.backupTime === null || settings.backupTime <= 0 || settings.voltage === null || ![12, 24, 48].includes(settings.voltage) || settings.efficiency === null || settings.efficiency < 50 || settings.efficiency > 100 || (runtimeBatteryValue !== undefined && runtimeBatteryValue !== null && settings.runtimeBatteryAh === null) || (settings.runtimeBatteryAh !== null && settings.runtimeBatteryAh < 0)) {
            throw invalidImport('ค่าตั้งค่าแบตเตอรี่ไม่ถูกต้อง');
        }
        if (data.forecast !== undefined && data.forecast !== null && (typeof data.forecast !== 'object' || Array.isArray(data.forecast))) {
            throw invalidImport('ค่า Forecast ไม่ถูกต้อง');
        }
        const forecastSource = data.forecast && typeof data.forecast === 'object' ? data.forecast : null;
        const legacyForecast = data.forecastWatt ?? data.calculatedData.forecastWatt ?? 0;
        const forecastValue = finiteNumber(forecastSource ? (forecastSource.value ?? 0) : legacyForecast);
        const forecastUnit = forecastSource ? (forecastSource.unit ?? '%') : (legacyForecast ? 'W' : '%');
        if (forecastValue === null || forecastValue < 0 || !['%', 'W'].includes(forecastUnit)) throw invalidImport('ค่า Forecast ไม่ถูกต้อง');
        const breakdown = calculateLoadBreakdown(devices, forecastValue, forecastUnit);
        if (!breakdown.valid) throw invalidImport('โหลดรวมอยู่นอกขอบเขตที่คำนวณได้');
        if (breakdown.totalLoad > 0 && calculateBatteryAh(breakdown.totalLoad, settings.backupTime, settings.voltage, settings.efficiency) === null) {
            throw invalidImport('ค่าตั้งค่าแบตเตอรี่ทำให้คำนวณ Ah ไม่ได้');
        }
        if (settings.runtimeBatteryAh !== null && breakdown.totalLoad > 0 && calculateRuntimeMinutes(settings.runtimeBatteryAh, settings.voltage, settings.efficiency, breakdown.totalLoad) === null) {
            throw invalidImport('ค่าความจุแบตเตอรี่ทำให้คำนวณ Runtime ไม่ได้');
        }
        return { devices, settings, forecast: { value: forecastValue, unit: forecastUnit }, mode };
    }

    function buildExportData({ devices, calculatedData, settings, calculationSettings = settings, runtimeBatteryWasAutoFilled = false, mode = null, exportDate = new Date() }) {
        const exportSettings = runtimeBatteryWasAutoFilled
            ? { ...settings, runtimeBatteryAh: null }
            : settings;
        const activeCalculationSettings = runtimeBatteryWasAutoFilled
            ? { ...calculationSettings, runtimeBatteryAh: null }
            : calculationSettings;
        const forecastValue = finiteNumber(calculatedData.forecastValue) ?? 0;
        const forecastUnit = calculatedData.forecastUnit || '%';
        const breakdown = calculateLoadBreakdown(devices, forecastValue, forecastUnit);
        const activeBackupTime = finiteNumber(activeCalculationSettings.backupTime);
        const activeVoltage = finiteNumber(activeCalculationSettings.voltage);
        const activeEfficiency = finiteNumber(activeCalculationSettings.efficiency);
        const batteryAh = breakdown.valid && breakdown.totalLoad > 0
            ? calculateBatteryAh(breakdown.totalLoad, activeBackupTime, activeVoltage, activeEfficiency)
            : null;
        const runtimeCapacityAh = activeCalculationSettings.runtimeBatteryAh ?? exportSettings.runtimeBatteryAh ?? batteryAh;
        const runtime = breakdown.valid && breakdown.totalLoad > 0
            ? calculateRuntimeMinutes(runtimeCapacityAh, activeVoltage, activeEfficiency, breakdown.totalLoad)
            : null;
        const exportCalculatedData = {
            ...calculatedData,
            deviceLoad: breakdown.valid ? breakdown.deviceLoad : 0,
            totalLoad: breakdown.valid ? breakdown.totalLoad : 0,
            totalPlugs: breakdown.valid ? breakdown.totalPlugs : 0,
            forecastValue,
            forecastUnit,
            forecastWatt: breakdown.valid ? breakdown.forecastWatt : 0,
            batteryAh,
            backupTime: activeBackupTime,
            voltage: activeVoltage,
            efficiency: activeEfficiency,
            runtime,
        };
        return {
            version: '1.4', appName: 'UPS Picker', exportDate: exportDate.toISOString(), mode,
            devices: devices.map(device => ({ ...device })),
            calculatedData: exportCalculatedData,
            forecast: { value: forecastValue, unit: forecastUnit, watt: exportCalculatedData.forecastWatt },
            settings: { ...exportSettings },
        };
    }

    return { calculateLoadBreakdown, calculateBatteryAh, calculateRuntimeMinutes, buildRuntimeCurve, recommendUpsVA, planOutlets, escapeHtml, normalizeImportData, buildExportData };
}));

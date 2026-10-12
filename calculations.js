(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.UPScalculations = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const DEFAULT_POWER_FACTOR = 0.8;
    const MAX_DEVICES = 500;
    const MAX_DEVICE_NAME_LENGTH = 160;
    const MAX_IMPORT_FILE_BYTES = 1_000_000;

    function utf8ByteLength(text) {
        let bytes = 0;
        for (let index = 0; index < text.length; index++) {
            const code = text.charCodeAt(index);
            if (code <= 0x7f) bytes++;
            else if (code <= 0x7ff) bytes += 2;
            else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length
                && text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) {
                bytes += 4;
                index++;
            } else bytes += 3;
        }
        return bytes;
    }

    function parseImportJson(jsonText) {
        if (typeof jsonText !== 'string' || utf8ByteLength(jsonText) > MAX_IMPORT_FILE_BYTES) {
            throw invalidImport('ไฟล์ JSON ไม่ถูกต้องหรือมีขนาดใหญ่เกินไป');
        }
        const source = jsonText.charCodeAt(0) === 0xfeff ? jsonText.slice(1) : jsonText;
        JSON.parse(source);
        let preserved = '';
        let inString = false;
        let escaped = false;
        const numberToken = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
        for (let index = 0; index < source.length; index++) {
            const character = source[index];
            if (inString) {
                preserved += character;
                if (escaped) escaped = false;
                else if (character === '\\') escaped = true;
                else if (character === '"') inString = false;
                continue;
            }
            if (character === '"') {
                inString = true;
                preserved += character;
                continue;
            }
            if (character === '-' || (character >= '0' && character <= '9')) {
                numberToken.lastIndex = index;
                const match = numberToken.exec(source);
                if (match) {
                    preserved += JSON.stringify(match[0]);
                    index += match[0].length - 1;
                    continue;
                }
            }
            preserved += character;
        }
        return JSON.parse(preserved);
    }

    function finiteNumber(value) {
        if (value === null || value === undefined || typeof value === 'boolean') return null;
        if (typeof value !== 'number' && typeof value !== 'string') return null;
        if (typeof value === 'string' && value.trim() === '') return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function decimalFraction(value) {
        if (typeof value !== 'number' && typeof value !== 'string') return null;
        const text = typeof value === 'string' ? value.trim() : Number.isFinite(value) ? value.toString() : '';
        if (text.length === 0 || text.length > 512) return null;
        const match = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:e([+-]?\d+))?$/i.exec(text);
        if (!match) return null;
        const fraction = match[4] ?? match[3] ?? '';
        const whole = match[2] || '0';
        const exponent = Number(match[5] || 0);
        if (!Number.isSafeInteger(exponent)) return null;
        let numerator = BigInt(`${whole}${fraction}`);
        let denominator = 1n;
        const scale = fraction.length - exponent;
        if (Math.abs(scale) > 1024) return null;
        if (scale > 0) denominator = 10n ** BigInt(scale);
        else if (scale < 0) numerator *= 10n ** BigInt(-scale);
        if (match[1] === '-') numerator = -numerator;
        return { numerator, denominator };
    }

    function decimalInputMatchesNumber(value, number) {
        const inputFraction = decimalFraction(value);
        const numberFraction = decimalFraction(number);
        return Boolean(inputFraction && numberFraction
            && inputFraction.numerator * numberFraction.denominator === numberFraction.numerator * inputFraction.denominator);
    }

    function canonicalNumericOrRaw(value) {
        const number = finiteNumber(value);
        return number !== null && decimalInputMatchesNumber(value, number) ? number : value;
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
            return { devices: [], deviceLoad: 0, forecastWatt: 0, totalLoad: 0, totalPlugs: 0, precisionInvalidTarget: null, invalidPlugIndex: null, valid: false };
        }
        const devices = [];
        let deviceLoad = 0;
        let deviceLoadMicroWatts = 0n;
        let totalPlugs = 0;
        let inputsValid = true;
        let precisionInvalidTarget = null;
        let invalidPlugIndex = null;
        inputDevices.forEach((device, index) => {
            const rawLoad = device && device.load;
            const load = finiteNumber(rawLoad);
            const loadFraction = decimalFraction(rawLoad);
            if (load === null) {
                if (rawLoad !== undefined && rawLoad !== null && !(typeof rawLoad === 'string' && rawLoad.trim() === '')) inputsValid = false;
                return;
            }
            if (!loadFraction || loadFraction.numerator < 0n) {
                inputsValid = false;
                return;
            }
            if (!decimalInputMatchesNumber(rawLoad, load)) {
                inputsValid = false;
                if (loadFraction.numerator > 0n) precisionInvalidTarget ??= { type: 'device-load', index };
                return;
            }
            const unit = device.unit === undefined || device.unit === null || device.unit === '' ? 'W' : device.unit;
            if (!['W', 'VA'].includes(unit)) {
                inputsValid = false;
                return;
            }
            const wattsNumerator = unit === 'VA' ? loadFraction.numerator * 4n : loadFraction.numerator;
            const wattsDenominator = unit === 'VA' ? loadFraction.denominator * 5n : loadFraction.denominator;
            const rawPlugs = device.plugs === undefined || device.plugs === null || device.plugs === '' ? 1 : device.plugs;
            const plugs = finiteNumber(rawPlugs);
            if (plugs === null || !decimalInputMatchesNumber(rawPlugs, plugs)
                || !Number.isSafeInteger(plugs) || plugs < 1 || !Number.isSafeInteger(totalPlugs + plugs)) {
                inputsValid = false;
                invalidPlugIndex ??= index;
                return;
            }
            const rowMicroWatts = roundFractionToScaledInteger(wattsNumerator, wattsDenominator, 6);
            const roundedWatts = rowMicroWatts === null ? null : fromScaledInteger(rowMicroWatts, 6);
            if (roundedWatts === null) {
                inputsValid = false;
                precisionInvalidTarget ??= { type: 'device-load', index };
                return;
            }
            if (loadFraction.numerator > 0n && roundedWatts === 0) {
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
        const parsedForecastFraction = forecastProvided ? decimalFraction(forecastValue) : decimalFraction(0);
        if ((forecastProvided && (rawForecast === null || !parsedForecastFraction || !decimalInputMatchesNumber(forecastValue, rawForecast)))
            || rawForecast < 0 || (parsedForecastFraction && parsedForecastFraction.numerator < 0n)) inputsValid = false;
        const forecastFraction = parsedForecastFraction || decimalFraction(0);
        const resolvedForecastUnit = forecastUnit === undefined || forecastUnit === null || forecastUnit === '' ? '%' : forecastUnit;
        if (!['%', 'W'].includes(resolvedForecastUnit)) inputsValid = false;
        const unit = resolvedForecastUnit === 'W' ? 'W' : '%';
        const forecastNumerator = unit === '%' ? deviceLoadMicroWatts * forecastFraction.numerator : forecastFraction.numerator;
        const forecastDenominator = unit === '%'
            ? 1_000_000n * forecastFraction.denominator * 100n
            : forecastFraction.denominator;
        const forecastMicroWatts = roundFractionToScaledInteger(forecastNumerator, forecastDenominator, 6);
        const roundedForecastWatt = forecastMicroWatts === null ? null : fromScaledInteger(forecastMicroWatts, 6);
        const forecastPositive = forecastFraction.numerator > 0n && (unit === 'W' || deviceLoad > 0);
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
            invalidPlugIndex,
            valid,
        };
    }

    function requiredBatteryAhFraction(powerW, minutes, voltage, efficiency) {
        const numericInputs = [powerW, minutes, voltage, efficiency].map(finiteNumber);
        if (numericInputs.some(value => value === null)
            || [powerW, minutes, voltage, efficiency].some((value, index) => !decimalInputMatchesNumber(value, numericInputs[index]))) return null;
        const p = decimalFraction(powerW), t = decimalFraction(minutes);
        const v = decimalFraction(voltage), e = decimalFraction(efficiency);
        if (!p || !t || !v || !e || p.numerator <= 0n || t.numerator < t.denominator || v.numerator <= 0n
            || e.numerator < 50n * e.denominator || e.numerator > 100n * e.denominator) return null;
        return {
            numerator: p.numerator * t.numerator * 100n * v.denominator * e.denominator,
            denominator: p.denominator * t.denominator * 60n * v.numerator * e.numerator,
        };
    }

    function calculateBatteryAh(powerW, minutes, voltage, efficiency) {
        const requirement = requiredBatteryAhFraction(powerW, minutes, voltage, efficiency);
        if (!requirement || requirement.numerator * 1_000_000n < requirement.denominator) return null;
        const scaledAh = (requirement.numerator * 1_000_000n + requirement.denominator - 1n) / requirement.denominator;
        return fromScaledInteger(scaledAh, 6);
    }

    function resolveActiveBatterySettings(mode, settings) {
        return mode === 'basic'
            ? { voltage: 12, efficiency: 85 }
            : { voltage: settings.voltage, efficiency: settings.efficiency };
    }

    function isBelowAhResolution(powerW, minutes, voltage, efficiency) {
        const requirement = requiredBatteryAhFraction(powerW, minutes, voltage, efficiency);
        return Boolean(requirement && requirement.numerator * 1_000_000n < requirement.denominator);
    }

    function isAhRequirementBeyondOutputRange(powerW, minutes, voltage, efficiency) {
        const requirement = requiredBatteryAhFraction(powerW, minutes, voltage, efficiency);
        if (!requirement || requirement.numerator * 1_000_000n < requirement.denominator) return false;
        const scaledAh = (requirement.numerator * 1_000_000n + requirement.denominator - 1n) / requirement.denominator;
        return fromScaledInteger(scaledAh, 6) === null;
    }

    function calculateRuntimeMinutes(ampHours, voltage, efficiency, powerW) {
        const numericInputs = [ampHours, voltage, efficiency, powerW].map(finiteNumber);
        if (numericInputs.some(value => value === null)
            || [ampHours, voltage, efficiency, powerW].some((value, index) => !decimalInputMatchesNumber(value, numericInputs[index]))) return null;
        const a = decimalFraction(ampHours), v = decimalFraction(voltage);
        const e = decimalFraction(efficiency), p = decimalFraction(powerW);
        if (!a || !v || !e || !p || a.numerator < 0n || v.numerator <= 0n || p.numerator <= 0n
            || e.numerator < 50n * e.denominator || e.numerator > 100n * e.denominator) return null;
        const ah = finiteNumber(ampHours), volts = finiteNumber(voltage);
        const percent = finiteNumber(efficiency), power = finiteNumber(powerW);
        const numerator = a.numerator * v.numerator * e.numerator * 60n * p.denominator;
        const denominator = a.denominator * v.denominator * e.denominator * 100n * p.numerator;
        const hundredthsNumerator = numerator * 100n;
        const roundedHundredths = (hundredthsNumerator * 2n + denominator) / (denominator * 2n);
        if (ah > 0 && roundedHundredths === 0n) {
            let exactMinutes = (ah / power) * volts * (percent / 100) * 60;
            if (!Number.isFinite(exactMinutes) || exactMinutes === 0) {
                exactMinutes = Math.exp(Math.log(ah) - Math.log(power) + Math.log(volts) + Math.log(percent / 100) + Math.log(60));
            }
            return Number.isFinite(exactMinutes) && exactMinutes > 0 ? exactMinutes : null;
        }
        return fromScaledInteger(roundedHundredths, 2);
    }

    function buildRuntimeCurve(ampHours, voltage, efficiency, currentLoadW) {
        const current = finiteNumber(currentLoadW);
        if (current === null || !decimalInputMatchesNumber(currentLoadW, current) || current <= 0
            || calculateRuntimeMinutes(ampHours, voltage, efficiency, current) === null) return [];
        const loads = [...new Set([50, 100, 150, 200, 250, 300, current])].sort((a, b) => a - b);
        return loads.map(loadW => ({
            loadW,
            runtimeMinutes: calculateRuntimeMinutes(ampHours, voltage, efficiency, loadW),
            currentLoad: loadW === current,
        })).filter(point => Number.isFinite(point.runtimeMinutes));
    }

    function recommendUpsVA(loadW) {
        const load = finiteNumber(loadW);
        if (load === null) return null;
        const watts = decimalFraction(loadW);
        if (!watts || !decimalInputMatchesNumber(loadW, load)) return null;
        if (watts.numerator <= 0n) return 0;
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

    function invalidRuntimeCurve(message) {
        const error = new Error(message);
        error.name = 'InvalidRuntimeCurveError';
        return error;
    }

    function runtimeCurveImportFailureMessage(error, existingModel) {
        const details = error && typeof error.message === 'string' ? error.message : 'ตรวจสอบไฟล์และข้อมูลรุ่น';
        const retainedCurve = typeof existingModel === 'string' && existingModel.trim()
            ? `ยังใช้เส้นโค้งเดิมของ ${existingModel} ต่อ`
            : 'ยังไม่มีเส้นโค้งที่ใช้งาน';
        return `นำเข้าไม่ได้: ${details} · ${retainedCurve}`;
    }

    function normalizeRuntimeCurvePoints(points) {
        if (!Array.isArray(points) || points.length < 2 || points.length > 1000) return null;
        const normalized = [];
        for (const point of points) {
            if (!point || typeof point !== 'object' || Array.isArray(point)) return null;
            const loadW = finiteNumber(point.loadW);
            const runtimeMinutes = finiteNumber(point.runtimeMinutes);
            if (loadW === null || runtimeMinutes === null
                || !decimalInputMatchesNumber(point.loadW, loadW)
                || !decimalInputMatchesNumber(point.runtimeMinutes, runtimeMinutes)
                || loadW <= 0 || runtimeMinutes <= 0
                || loadW >= 1e21 || runtimeMinutes >= 1e21) return null;
            normalized.push({ loadW, runtimeMinutes });
        }
        normalized.sort((a, b) => a.loadW - b.loadW);
        for (let index = 1; index < normalized.length; index++) {
            if (normalized[index].loadW <= normalized[index - 1].loadW
                || normalized[index].runtimeMinutes > normalized[index - 1].runtimeMinutes) return null;
        }
        return normalized;
    }

    function parseRuntimeCurveCsv(csvText) {
        if (typeof csvText !== 'string' || utf8ByteLength(csvText) > 256_000) throw invalidRuntimeCurve('ไฟล์ CSV ไม่ถูกต้องหรือมีขนาดใหญ่เกินไป');
        const records = [];
        let row = [];
        let field = '';
        let inQuotes = false;
        let afterQuote = false;
        const commitField = () => {
            row.push(field.trim());
            field = '';
            afterQuote = false;
        };
        const commitRow = () => {
            commitField();
            if (row.some(value => value !== '')) records.push(row);
            row = [];
        };
        const source = csvText.replace(/^\uFEFF/, '');
        for (let index = 0; index < source.length; index++) {
            const character = source[index];
            if (inQuotes) {
                if (character === '"' && source[index + 1] === '"') {
                    field += '"';
                    index++;
                } else if (character === '"') {
                    inQuotes = false;
                    afterQuote = true;
                } else {
                    field += character;
                }
                continue;
            }
            if (character === ',') {
                commitField();
            } else if (character === '\n' || character === '\r') {
                if (character === '\r' && source[index + 1] === '\n') index++;
                commitRow();
            } else if (character === '"') {
                if (field.trim() !== '' || afterQuote) throw invalidRuntimeCurve('รูปแบบเครื่องหมายคำพูดใน CSV ไม่ถูกต้อง');
                field = '';
                inQuotes = true;
            } else if (afterQuote) {
                if (!/\s/.test(character)) throw invalidRuntimeCurve('รูปแบบเครื่องหมายคำพูดใน CSV ไม่ถูกต้อง');
            } else {
                field += character;
            }
        }
        if (inQuotes) throw invalidRuntimeCurve('รูปแบบเครื่องหมายคำพูดใน CSV ไม่ถูกต้อง');
        if (row.length > 0 || field !== '' || afterQuote) commitRow();
        if (records.length < 3 || records.length > 1001) throw invalidRuntimeCurve('CSV ต้องมีอย่างน้อย 2 จุดและไม่เกิน 1000 จุด');
        const header = records[0].map(value => value.toLowerCase());
        if (header.length !== 2 || header[0] !== 'load_w' || header[1] !== 'runtime_minutes') {
            throw invalidRuntimeCurve('หัวตารางต้องเป็น load_w,runtime_minutes');
        }
        const rows = records.slice(1).map(values => {
            if (values.length !== 2) throw invalidRuntimeCurve('แต่ละแถวต้องมี Load W และ Runtime นาที');
            return { loadW: values[0], runtimeMinutes: values[1] };
        });
        const points = normalizeRuntimeCurvePoints(rows);
        if (!points) throw invalidRuntimeCurve('จุดข้อมูลต้องเป็นตัวเลขบวก โหลดไม่ซ้ำ และ Runtime ต้องไม่เพิ่มเมื่อโหลดสูงขึ้น');
        return points;
    }

    function normalizeRuntimeCurveData(curve) {
        if (curve === undefined || curve === null) return null;
        if (typeof curve !== 'object' || Array.isArray(curve)) throw invalidRuntimeCurve('ข้อมูลกราฟ Runtime ไม่ถูกต้อง');
        const model = typeof curve.model === 'string' ? curve.model.trim() : '';
        const batteryConfig = typeof curve.batteryConfig === 'string' ? curve.batteryConfig.trim() : '';
        const source = typeof curve.source === 'string' ? curve.source.trim() : '';
        const conditions = curve.conditions === undefined || curve.conditions === null
            ? '' : typeof curve.conditions === 'string' ? curve.conditions.trim() : null;
        const points = normalizeRuntimeCurvePoints(curve.points);
        if (!model || model.length > 160 || !batteryConfig || batteryConfig.length > 240
            || !source || source.length > 2048 || conditions === null || conditions.length > 1024 || !points) {
            throw invalidRuntimeCurve('ระบุรุ่น UPS ชุดแบตเตอรี่ แหล่งข้อมูล และจุด Runtime ให้ถูกต้อง');
        }
        return { model, batteryConfig, source, conditions, points };
    }

    function runtimeCurveMetadataMatches(curve, metadata) {
        if (!curve || !metadata) return false;
        return ['model', 'batteryConfig', 'source', 'conditions'].every(key =>
            (typeof curve[key] === 'string' ? curve[key].trim() : '')
            === (typeof metadata[key] === 'string' ? metadata[key].trim() : '')
        );
    }

    function calculateRuntimeFromCurve(inputPoints, currentLoadW, preservePrecision = false) {
        const points = normalizeRuntimeCurvePoints(inputPoints);
        const load = finiteNumber(currentLoadW);
        if (!points || load === null || !decimalInputMatchesNumber(currentLoadW, load) || load < points[0].loadW
            || load > points[points.length - 1].loadW) return null;
        const exactPoint = points.find(point => point.loadW === load);
        if (exactPoint) return exactPoint.runtimeMinutes;
        let upperIndex = points.findIndex(point => point.loadW > load);
        if (upperIndex <= 0) return null;
        const lower = points[upperIndex - 1], upper = points[upperIndex];
        const loadFraction = decimalFraction(load);
        const lowerLoad = decimalFraction(lower.loadW), upperLoad = decimalFraction(upper.loadW);
        const lowerRuntime = decimalFraction(lower.runtimeMinutes), upperRuntime = decimalFraction(upper.runtimeMinutes);
        const distanceNumerator = loadFraction.numerator * lowerLoad.denominator - lowerLoad.numerator * loadFraction.denominator;
        const distanceDenominator = loadFraction.denominator * lowerLoad.denominator;
        const spanNumerator = upperLoad.numerator * lowerLoad.denominator - lowerLoad.numerator * upperLoad.denominator;
        const spanDenominator = upperLoad.denominator * lowerLoad.denominator;
        const runtimeDropNumerator = lowerRuntime.numerator * upperRuntime.denominator - upperRuntime.numerator * lowerRuntime.denominator;
        const runtimeDropDenominator = lowerRuntime.denominator * upperRuntime.denominator;
        const resultNumerator = lowerRuntime.numerator * runtimeDropDenominator * spanNumerator * distanceDenominator
            - runtimeDropNumerator * distanceNumerator * lowerRuntime.denominator * spanDenominator;
        const resultDenominator = lowerRuntime.denominator * runtimeDropDenominator * spanNumerator * distanceDenominator;
        const preciseRuntime = () => {
            // Convert the exact fraction with 17 significant decimal digits, including subnormal times.
            let exponent = resultNumerator.toString().length - resultDenominator.toString().length;
            const belowPower = exponent >= 0
                ? resultNumerator < resultDenominator * (10n ** BigInt(exponent))
                : resultNumerator * (10n ** BigInt(-exponent)) < resultDenominator;
            if (belowPower) exponent--;
            const places = Math.max(0, 16 - exponent);
            const preciseScaled = roundFractionToScaledInteger(resultNumerator, resultDenominator, places);
            const precise = Number(scaledIntegerText(preciseScaled, places));
            return Number.isFinite(precise) && precise > 0 ? precise : null;
        };
        if (preservePrecision) return preciseRuntime();
        const scaled = roundFractionToScaledInteger(resultNumerator, resultDenominator, 2);
        if (scaled === null) return null;
        if (scaled > 0n) return fromScaledInteger(scaled, 2) ?? preciseRuntime();
        return preciseRuntime();
    }

    function buildManufacturerRuntimeCurve(inputPoints, currentLoadW) {
        const points = normalizeRuntimeCurvePoints(inputPoints);
        const current = finiteNumber(currentLoadW);
        if (!points || current === null || !decimalInputMatchesNumber(currentLoadW, current)
            || calculateRuntimeFromCurve(points, current) === null) return [];
        const curve = points.map(point => ({ ...point, currentLoad: point.loadW === current }));
        if (!curve.some(point => point.currentLoad)) {
            curve.push({
                loadW: current,
                runtimeMinutes: calculateRuntimeFromCurve(points, current, true),
                currentLoad: true,
            });
            curve.sort((a, b) => a.loadW - b.loadW);
        }
        return curve;
    }

    function normalizeImportData(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(data.devices) || !data.calculatedData || typeof data.calculatedData !== 'object' || Array.isArray(data.calculatedData)) {
            throw invalidImport('รูปแบบไฟล์ไม่ถูกต้อง');
        }
        if (data.devices.length > MAX_DEVICES) throw invalidImport(`ไฟล์มีอุปกรณ์เกินขีดจำกัด ${MAX_DEVICES} รายการ`);
        if (data.version !== undefined && data.version !== null) {
            if (typeof data.version !== 'string' || !/^1\.[0-5]$/.test(data.version)) {
                throw invalidImport(`ไม่รองรับเวอร์ชันไฟล์ ${String(data.version)}`);
            }
        }
        let mode = data.mode === undefined || data.mode === null ? null : data.mode;
        if (mode !== null && !['basic', 'enterprise'].includes(mode)) throw invalidImport('โหมดการใช้งานไม่ถูกต้อง');
        const runtimeSource = data.runtimeSource === undefined || data.runtimeSource === null ? 'formula' : data.runtimeSource;
        if (!['formula', 'manufacturer'].includes(runtimeSource)) throw invalidImport('แหล่งข้อมูล Runtime ไม่ถูกต้อง');
        let runtimeCurve;
        try {
            runtimeCurve = normalizeRuntimeCurveData(data.runtimeCurve);
        } catch (error) {
            throw invalidImport('ข้อมูลกราฟ Runtime ไม่ถูกต้อง');
        }
        if (runtimeSource === 'manufacturer' && !runtimeCurve) throw invalidImport('ต้องแนบกราฟ Runtime ของผู้ผลิต');
        const devices = data.devices.map(device => {
            if (!device || typeof device !== 'object' || Array.isArray(device)) throw invalidImport('ข้อมูลอุปกรณ์ไม่ถูกต้อง');
            if (device.name !== undefined && typeof device.name !== 'string') throw invalidImport('ชื่ออุปกรณ์ไม่ถูกต้อง');
            if (typeof device.name === 'string' && device.name.length > MAX_DEVICE_NAME_LENGTH) {
                throw invalidImport(`ชื่ออุปกรณ์ยาวเกิน ${MAX_DEVICE_NAME_LENGTH} ตัวอักษร`);
            }
            const rawLoad = device.load;
            const load = finiteNumber(rawLoad);
            const loadFraction = decimalFraction(rawLoad);
            const rawPlugs = device.plugs === undefined ? 1 : device.plugs;
            const plugs = finiteNumber(rawPlugs);
            if (load === null || !loadFraction || !decimalInputMatchesNumber(rawLoad, load)
                || (typeof rawLoad === 'number' && Number.isInteger(load) && !Number.isSafeInteger(load)) || loadFraction.numerator < 0n
                || plugs === null || !decimalInputMatchesNumber(rawPlugs, plugs) || !Number.isSafeInteger(plugs)
                || plugs < 1 || !['W', 'VA'].includes(device.unit ?? 'W')) {
                throw invalidImport('ข้อมูลโหลดหรือจำนวนปลั๊กไม่ถูกต้อง');
            }
            return { name: String(device.name ?? ''), load, unit: device.unit ?? 'W', plugs };
        });
        const sourceSettings = data.settings === undefined || data.settings === null ? {} : data.settings;
        if (typeof sourceSettings !== 'object' || Array.isArray(sourceSettings)) throw invalidImport('ค่าตั้งค่าแบตเตอรี่ไม่ถูกต้อง');
        const runtimeBatteryValue = sourceSettings.runtimeBatteryAh;
        const runtimeBatteryAh = runtimeBatteryValue === undefined || runtimeBatteryValue === null
            ? null : finiteNumber(runtimeBatteryValue);
        const rawBackupTime = sourceSettings.backupTime ?? data.calculatedData.backupTime ?? 60;
        const rawVoltage = sourceSettings.voltage ?? data.calculatedData.voltage ?? 12;
        const rawEfficiency = sourceSettings.efficiency ?? data.calculatedData.efficiency ?? 85;
        const backupTimeFraction = decimalFraction(rawBackupTime);
        const voltageFraction = decimalFraction(rawVoltage);
        const efficiencyFraction = decimalFraction(rawEfficiency);
        const runtimeBatteryFraction = runtimeBatteryValue === undefined || runtimeBatteryValue === null
            ? null : decimalFraction(runtimeBatteryValue);
        const settings = {
            backupTime: finiteNumber(rawBackupTime),
            voltage: finiteNumber(rawVoltage),
            efficiency: finiteNumber(rawEfficiency),
            runtimeBatteryAh,
        };
        const supportedVoltage = voltageFraction && [12n, 24n, 48n].some(value => voltageFraction.numerator === value * voltageFraction.denominator);
        if (settings.backupTime === null || !decimalInputMatchesNumber(rawBackupTime, settings.backupTime) || settings.backupTime < 1
            || !supportedVoltage || settings.voltage === null || !decimalInputMatchesNumber(rawVoltage, settings.voltage)
            || settings.efficiency === null || !decimalInputMatchesNumber(rawEfficiency, settings.efficiency)
            || !efficiencyFraction || efficiencyFraction.numerator < 50n * efficiencyFraction.denominator || efficiencyFraction.numerator > 100n * efficiencyFraction.denominator
            || (runtimeBatteryValue !== undefined && runtimeBatteryValue !== null && (settings.runtimeBatteryAh === null || !runtimeBatteryFraction || !decimalInputMatchesNumber(runtimeBatteryValue, settings.runtimeBatteryAh) || runtimeBatteryFraction.numerator < 0n))) {
            throw invalidImport('ค่าตั้งค่าแบตเตอรี่ไม่ถูกต้อง');
        }
        if (data.forecast !== undefined && data.forecast !== null && (typeof data.forecast !== 'object' || Array.isArray(data.forecast))) {
            throw invalidImport('ค่า Forecast ไม่ถูกต้อง');
        }
        const forecastSource = data.forecast && typeof data.forecast === 'object' ? data.forecast : null;
        const legacyForecast = data.forecastWatt ?? data.calculatedData.forecastWatt ?? 0;
        const hasForecastValue = Boolean(forecastSource && forecastSource.value !== undefined && forecastSource.value !== null);
        const hasForecastWatt = Boolean(forecastSource && forecastSource.watt !== undefined && forecastSource.watt !== null);
        const rawForecastValue = hasForecastValue
            ? forecastSource.value
            : hasForecastWatt ? forecastSource.watt : legacyForecast;
        const forecastNumber = finiteNumber(rawForecastValue);
        const forecastFraction = decimalFraction(rawForecastValue);
        const forecastUnit = hasForecastValue
            ? forecastSource.unit ?? '%'
            : hasForecastWatt || legacyForecast !== 0
                ? 'W'
                : forecastSource?.unit ?? '%';
        if (forecastNumber === null || !forecastFraction || !decimalInputMatchesNumber(rawForecastValue, forecastNumber)
            || (typeof rawForecastValue === 'number' && Number.isInteger(forecastNumber) && !Number.isSafeInteger(forecastNumber))
            || forecastFraction.numerator < 0n || !['%', 'W'].includes(forecastUnit)) {
            throw invalidImport('ค่า Forecast ไม่ถูกต้อง');
        }
        const breakdown = calculateLoadBreakdown(devices, rawForecastValue, forecastUnit);
        if (!breakdown.valid) throw invalidImport('โหลดรวมอยู่นอกขอบเขตที่คำนวณได้');
        if (mode === null) {
            const hasEnterpriseMarkers = runtimeSource === 'manufacturer'
                || devices.some(device => device.unit === 'VA' || device.plugs !== 1)
                || forecastNumber > 0;
            const hasSavedEnterpriseSettings = settings.voltage !== 12 || settings.efficiency !== 85;
            const calculatedVoltage = finiteNumber(data.calculatedData.voltage);
            const calculatedEfficiency = finiteNumber(data.calculatedData.efficiency);
            const hasValidCalculatedSettings = calculatedVoltage !== null && calculatedEfficiency !== null
                && decimalInputMatchesNumber(data.calculatedData.voltage, calculatedVoltage)
                && decimalInputMatchesNumber(data.calculatedData.efficiency, calculatedEfficiency)
                && [12, 24, 48].includes(calculatedVoltage)
                && calculatedEfficiency >= 50 && calculatedEfficiency <= 100;
            const hasBasicCalculatedSettings = decimalInputMatchesNumber(data.calculatedData.voltage, 12)
                && decimalInputMatchesNumber(data.calculatedData.efficiency, 85);
            const hasEnterpriseCalculatedSettings = hasValidCalculatedSettings
                && (calculatedVoltage !== 12 || calculatedEfficiency !== 85);
            if (hasEnterpriseMarkers) mode = 'enterprise';
            else if (hasSavedEnterpriseSettings && hasBasicCalculatedSettings) mode = 'basic';
            else if (hasSavedEnterpriseSettings || hasEnterpriseCalculatedSettings) mode = 'enterprise';
            else mode = 'basic';
            if (mode === 'enterprise' && !hasSavedEnterpriseSettings && hasEnterpriseCalculatedSettings) {
                settings.voltage = calculatedVoltage;
                settings.efficiency = calculatedEfficiency;
            }
        }
        if (runtimeSource === 'manufacturer' && mode !== 'enterprise') {
            throw invalidImport('ต้องใช้โหมด Enterprise กับกราฟ Runtime ของผู้ผลิต');
        }
        const activeSettings = resolveActiveBatterySettings(mode, settings);
        if (breakdown.totalLoad > 0 && calculateBatteryAh(breakdown.totalLoad, settings.backupTime, activeSettings.voltage, activeSettings.efficiency) === null) {
            const belowResolution = isBelowAhResolution(breakdown.totalLoad, settings.backupTime, activeSettings.voltage, activeSettings.efficiency);
            const beyondOutputRange = isAhRequirementBeyondOutputRange(breakdown.totalLoad, settings.backupTime, activeSettings.voltage, activeSettings.efficiency);
            throw invalidImport(belowResolution
                ? 'ค่าความจุที่ต้องการต่ำกว่า 0.000001 Ah ซึ่งเป็นความละเอียดขั้นต่ำที่รองรับ'
                : beyondOutputRange
                    ? 'ค่าความจุ Ah เกินขอบเขตตัวเลขที่แอปรองรับ'
                    : 'ค่าตั้งค่าแบตเตอรี่ทำให้คำนวณ Ah ไม่ได้');
        }
        if (runtimeSource === 'manufacturer' && breakdown.totalLoad > 0
            && calculateRuntimeFromCurve(runtimeCurve.points, breakdown.totalLoad) === null) {
            throw invalidImport('โหลดอยู่นอกช่วงกราฟ Runtime ของผู้ผลิต');
        }
        if (runtimeSource === 'formula' && settings.runtimeBatteryAh !== null && breakdown.totalLoad > 0
            && calculateRuntimeMinutes(settings.runtimeBatteryAh, activeSettings.voltage, activeSettings.efficiency, breakdown.totalLoad) === null) {
            throw invalidImport('ค่าความจุแบตเตอรี่ทำให้คำนวณ Runtime ไม่ได้');
        }
        return { devices, settings, forecast: { value: forecastNumber, unit: forecastUnit }, mode, runtimeSource, runtimeCurve };
    }

    function buildExportData({ devices, calculatedData, settings, calculationSettings = settings, runtimeBatteryWasAutoFilled = false, mode = null, runtimeSource = 'formula', runtimeCurve = null, exportDate = new Date() }) {
        const hasEnterpriseMarkers = runtimeSource === 'manufacturer'
            || finiteNumber(settings.voltage) !== 12 || finiteNumber(settings.efficiency) !== 85
            || devices.some(device => device.unit === 'VA' || device.plugs !== 1)
            || (calculatedData.forecastValue ?? 0) > 0;
        const exportMode = mode ?? (hasEnterpriseMarkers ? 'enterprise' : 'basic');
        const exportRuntimeSource = exportMode === 'basic' ? 'formula' : runtimeSource;
        const exportSettings = runtimeBatteryWasAutoFilled
            ? { ...settings, runtimeBatteryAh: null }
            : settings;
        const activeCalculationSettings = runtimeBatteryWasAutoFilled
            ? { ...calculationSettings, runtimeBatteryAh: null }
            : calculationSettings;
        const activeBatterySettings = resolveActiveBatterySettings(exportMode, activeCalculationSettings);
        const rawForecastValue = calculatedData.forecastValue ?? 0;
        const forecastValue = canonicalNumericOrRaw(rawForecastValue);
        const forecastUnit = calculatedData.forecastUnit || '%';
        const breakdown = calculateLoadBreakdown(devices, forecastValue, forecastUnit);
        const rawBackupTime = activeCalculationSettings.backupTime;
        const rawVoltage = activeBatterySettings.voltage;
        const rawEfficiency = activeBatterySettings.efficiency;
        const activeBackupTime = canonicalNumericOrRaw(rawBackupTime);
        const activeVoltage = canonicalNumericOrRaw(rawVoltage);
        const activeEfficiency = canonicalNumericOrRaw(rawEfficiency);
        const normalizedRuntimeCurve = normalizeRuntimeCurveData(runtimeCurve);
        const batteryAh = breakdown.valid && breakdown.totalLoad > 0
            ? calculateBatteryAh(breakdown.totalLoad, rawBackupTime, rawVoltage, rawEfficiency)
            : null;
        const runtimeCapacityAh = activeCalculationSettings.runtimeBatteryAh ?? exportSettings.runtimeBatteryAh ?? batteryAh;
        const runtime = breakdown.valid && breakdown.totalLoad > 0
            ? exportRuntimeSource === 'manufacturer'
                ? normalizedRuntimeCurve ? calculateRuntimeFromCurve(normalizedRuntimeCurve.points, breakdown.totalLoad) : null
                : calculateRuntimeMinutes(runtimeCapacityAh, rawVoltage, rawEfficiency, breakdown.totalLoad)
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
        const serializedSettings = { ...exportSettings };
        for (const key of ['backupTime', 'voltage', 'efficiency', 'runtimeBatteryAh']) {
            if (serializedSettings[key] !== undefined && serializedSettings[key] !== null) {
                serializedSettings[key] = canonicalNumericOrRaw(serializedSettings[key]);
            }
        }
        return {
            version: '1.5', appName: 'UPS Picker', exportDate: exportDate.toISOString(),
            mode: exportMode, runtimeSource: exportRuntimeSource,
            devices: devices.map(device => ({ ...device })),
            calculatedData: exportCalculatedData,
            forecast: { value: forecastValue, unit: forecastUnit, watt: exportCalculatedData.forecastWatt },
            settings: serializedSettings,
            runtimeCurve: normalizedRuntimeCurve,
        };
    }

    return { calculateLoadBreakdown, calculateBatteryAh, resolveActiveBatterySettings, isBelowAhResolution, isAhRequirementBeyondOutputRange, calculateRuntimeMinutes, buildRuntimeCurve, parseRuntimeCurveCsv, calculateRuntimeFromCurve, buildManufacturerRuntimeCurve, runtimeCurveImportFailureMessage, normalizeRuntimeCurveData, runtimeCurveMetadataMatches, recommendUpsVA, planOutlets, escapeHtml, parseImportJson, normalizeImportData, buildExportData, MAX_DEVICES, MAX_DEVICE_NAME_LENGTH, MAX_IMPORT_FILE_BYTES };
}));

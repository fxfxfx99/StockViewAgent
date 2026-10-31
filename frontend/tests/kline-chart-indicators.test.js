import test from 'node:test';
import assert from 'node:assert/strict';
import { aggregateCandlesFromDaily, buildCandleVolumeChartOption as build, getKlineChartHeight } from '../src/klineChartOption.js';
import { macd, kdj } from '../src/klineIndicators.js';
import { normalizeIndicatorSettings } from '../src/klineIndicatorSettings.js';

const candles = Array.from({ length: 80 }, (_, i) => ({ t: 1704067200 + i * 86400, o: i + 1, c: i + 2 + Math.sin(i), h: i + 4, l: i, v: i * 100 + 10 }));
const series = (option, name) => option.series.find((item) => item.name === name);
const settings = (patch) => normalizeIndicatorSettings(patch);

test('legacy chart callers preserve default MA and volume; explicit preferences control every pane', () => {
  const old = build({ candles });
  assert.deepEqual(old.legend.data, ['K线', 'MA20', 'MA60', 'MA250', '成交量', 'VOL-MA5', 'VOL-MA10']);
  assert.equal(old.grid.length, 2);
  const plain = build({ candles, indicatorSettings: settings({ main: { ma: false, boll: false }, subIndicators: [] }) });
  assert.deepEqual(plain.series.map((row) => row.name), ['K线']);
  assert.equal(plain.grid.length, 1);
  assert.deepEqual(plain.axisPointer.link[0].xAxisIndex, [0]);
  assert(plain.dataZoom.every((zoom) => zoom.xAxisIndex.length === 1));
  assert(parseFloat(plain.grid[0].height) > 70);
});

test('BOLL overlays use selected period and multiplier without changing input or requesting data', () => {
  const original = structuredClone(candles);
  const opts = settings({ main: { ma: false, boll: true }, subIndicators: [], params: { boll: { period: 3, multiplier: 1 } } });
  const option = build({ candles, indicatorSettings: opts });
  const first = candles.slice(0, 3).map((row) => row.c);
  const mean = first.reduce((a, b) => a + b) / 3;
  const deviation = Math.sqrt(first.reduce((total, value) => total + (value - mean) ** 2, 0) / 3);
  assert.deepEqual(series(option, 'BOLL中轨').data.slice(0, 2), [null, null]);
  assert.equal(series(option, 'BOLL中轨').data[2], mean);
  assert.equal(series(option, 'BOLL上轨').data[2], mean + deviation);
  assert.equal(series(option, 'BOLL下轨').data[2], mean - deviation);
  assert.deepEqual(candles, original);
});

test('MACD, KDJ and volume get separate scales and one synchronized zoom', () => {
  const preferences = settings({ subIndicators: ['macd', 'kdj', 'vol'], params: { macd: { fast: 3, slow: 6, signal: 2 }, kdj: { period: 4, k: 2, d: 2 } } });
  const option = build({ candles, indicatorSettings: preferences, showVolume: false });
  assert.equal(option.grid.length, 4);
  assert.equal(option.xAxis.length, 4);
  assert.equal(option.yAxis.length, 4);
  assert.equal(option.yAxis[1].scale, false, 'MACD bars must include their zero baseline');
  assert.equal(option.yAxis[3].scale, false, 'volume bars must include zero');
  assert.equal(option.yAxis[2].min({ min: -40 }), -40, 'negative J stays visible');
  assert.equal(option.yAxis[2].max({ max: 130 }), 130, 'J above 100 stays visible');
  assert.deepEqual(option.axisPointer.link[0].xAxisIndex, [0, 1, 2, 3]);
  assert(option.dataZoom.every((zoom) => JSON.stringify(zoom.xAxisIndex) === '[0,1,2,3]'));
  assert.equal(series(option, 'DIF').yAxisIndex, 1);
  assert.equal(series(option, 'K').yAxisIndex, 2);
  assert.equal(series(option, '成交量').yAxisIndex, 3);
  assert.deepEqual(series(option, 'DIF').data, macd(candles.map((row) => row.c), 3, 6, 2).dif);
  assert.deepEqual(series(option, 'K').data, kdj(candles.map((row) => row.h), candles.map((row) => row.l), candles.map((row) => row.c), 4, 2, 2).k);
  assert.match(option.title[0].text, /3, 6, 2/);
  assert.match(option.title[1].text, /4, 2, 2/);
  assert(option.xAxis.slice(0, 3).every((axis) => !axis.axisLabel.show));
  assert(option.xAxis[3].axisLabel.show);
});

test('custom MA and RSI use the chosen periods and recalculate from the full loaded history', () => {
  const preferences = settings({ main: { ma: true }, subIndicators: ['rsi'], params: { ma: { periods: [5, 10] }, rsi: { periods: [7, 14] } } });
  const option = build({ candles, defaultShowBars: 10, indicatorSettings: preferences });
  assert(series(option, 'MA5'));
  assert(series(option, 'MA10'));
  assert(!series(option, 'MA20'));
  assert(series(option, 'RSI7'));
  assert(series(option, 'RSI14'));
  assert.equal(option.yAxis[1].min, 0);
  assert.equal(option.yAxis[1].max, 100);
  assert.equal(series(option, 'MA10').data.length, candles.length);
  assert.notEqual(series(option, 'MA10').data[70], null);
});

test('weekly aggregation does not invent missing volume, amount, turnover or OHLC', () => {
  const missingVolume = candles.slice(0, 2).map(({ v, ...c }) => c);
  const [weekly] = aggregateCandlesFromDaily(missingVolume, '1wk');
  assert.equal(weekly.v, null);
  assert.equal(weekly.amount, null);
  assert.equal(weekly.turnover_rate, null);
  assert.equal(weekly.c, candles[1].c);
  const invalid = aggregateCandlesFromDaily([{ ...candles[0], c: null }, candles[1]], '1wk');
  assert.equal(invalid[0].c, null);
  const option = build({ candles: missingVolume, aggregateFromDaily: true, viewMode: '1wk', indicatorSettings: settings({ subIndicators: ['vol', 'macd'] }) });
  assert.equal(series(option, '成交量').data[0], null);
});

test('tooltips show BOLL values and missing-warmup status; gap is not a zero-price candle', () => {
  const option = build({ candles: [{ ...candles[0], c: ' ' }], indicatorSettings: settings({ main: { ma: false, boll: true }, subIndicators: ['macd'] }) });
  assert.deepEqual(series(option, 'K线').data[0], ['-', '-', '-', '-']);
  assert.deepEqual(series(option, 'BOLL中轨').data, [null]);
  const tooltip = option.tooltip.formatter([{ dataIndex: 0 }]);
  assert.match(tooltip, /BOLL中轨: —/);
  assert.match(tooltip, /MACD: —（数据不足）/);
  assert(!/NaN|Infinity/.test(tooltip));
  const invalid = build({ candles: [{ ...candles[0], c: NaN, o: Infinity }] });
  assert(!/NaN|Infinity/.test(invalid.tooltip.formatter([{ dataIndex: 0 }])));
});

test('multi-pane heights grow and compact charts keep valid axes', () => {
  assert.equal(getKlineChartHeight(520, settings({ subIndicators: ['vol'] })), 520);
  assert.equal(getKlineChartHeight(520, settings({ subIndicators: ['vol', 'macd', 'kdj'] })), 840);
  const option = build({ candles, layout: 'mini', showVolume: false });
  assert.equal(option.grid.length, 1);
  assert(option.series.every((row) => !row.yAxisIndex));
});

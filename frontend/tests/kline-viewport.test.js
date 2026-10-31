import test from 'node:test';
import assert from 'node:assert/strict';
import { applyKlineViewport, captureKlineViewport, resolveKlineViewport } from '../src/klineViewport.js';
const bars = Array.from({length:101}, (_, i) => ({t:1700000000+i*86400}));

test('historical zoom retains trading timestamps when an earlier history prefix is added', () => {
  const saved = captureKlineViewport({batch:[{start:20,end:60}]}, bars);
  const longer = [{t:bars[0].t-86400}, ...bars];
  const window = resolveKlineViewport(saved, longer);
  assert.equal(Math.round(window.start/100*101), 21);
  assert.equal(Math.round(window.end/100*101), 61);
  assert.equal(saved.followLatest, false);
});
test('right-anchored zoom follows replay steps without revealing hidden bars', () => {
  const saved = captureKlineViewport({start:80,end:100}, bars);
  const advanced = [...bars, {t:bars.at(-1).t+86400}];
  assert.equal(saved.barCount,21);
  assert.equal(resolveKlineViewport(saved, advanced).end,100);
  assert.equal(Math.round(resolveKlineViewport(saved, advanced).start/100*101),81);
  assert.deepEqual(resolveKlineViewport(saved, bars.slice(0,10)), {start:0,end:100});
});
test('rewinding clamps historical windows to the visible prefix', () => {
  const saved = captureKlineViewport({start:60,end:80}, bars);
  assert.deepEqual(resolveKlineViewport(saved,bars.slice(0,20)),{start:100,end:100});
});
test('unmodified focus options survive; recent/all override every linked pane', () => {
  const option={dataZoom:[{start:10,end:20,startValue:10,endValue:20},{start:10,end:20}],series:[]};
  assert.equal(applyKlineViewport(option,null,bars),option);
  const recent=applyKlineViewport(option,{recent:40},bars);
  assert(recent.dataZoom.every(x=>x.start===61&&x.end===100&&x.startValue===undefined));
  const all=applyKlineViewport(option,{all:true},bars);
  assert(all.dataZoom.every(x=>x.start===0&&x.end===100));
  assert.equal(option.dataZoom[0].startValue,10);
});
test('empty and malformed zoom events do not corrupt saved windows', () => {
  assert.equal(captureKlineViewport({},bars),null);
  assert.equal(captureKlineViewport({start:0,end:100},[]),null);
  assert.equal(resolveKlineViewport({all:true},[]),null);
});

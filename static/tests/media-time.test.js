const {test}=require('node:test');
const assert=require('node:assert/strict');
const {formatTime,parseTime}=require('../media-time.js');
test('format and parse video timestamps',()=>{
  assert.equal(formatTime(70,true),'00:01:10');
  assert.equal(formatTime(3661.125,true),'01:01:01.125');
  assert.equal(formatTime(3661.125),'01:01:01.125');
  assert.equal(parseTime('00:01:10'),70);
  assert.equal(parseTime('1:01:01.125'),3661.125);
  assert.equal(parseTime('1:10'),70);
  assert.ok(Number.isNaN(parseTime('00:99:10')));
  assert.ok(Number.isNaN(parseTime('70')));
  assert.ok(Number.isNaN(parseTime('00:00:60')));
});

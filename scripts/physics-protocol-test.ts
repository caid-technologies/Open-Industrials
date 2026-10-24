import assert from 'node:assert/strict';
import { decodePoseFrame } from '../src/lib/physics-protocol';

const packet = new ArrayBuffer(52), d = new DataView(packet);
d.setUint32(0, 0x3150494f, true); d.setUint32(4, 1, true); d.setUint32(8, 123, true); d.setUint32(12, 1, true); d.setFloat64(16, 0.246, true);
[1.23456789, 2, -3, 0, 0, 0, 1].forEach((n,i) => d.setFloat32(24+i*4,n,true));
const frame = decodePoseFrame(packet, 1);
assert.equal(frame.step,123); assert.equal(frame.time,.246); assert.equal(frame.poses[0],Math.fround(1.23456789));
assert.throws(() => decodePoseFrame(packet,2));
assert.throws(() => decodePoseFrame(packet.slice(0,51),1));
d.setFloat32(24,NaN,true);assert.throws(() => decodePoseFrame(packet,1));
d.setFloat32(24,1,true);d.setFloat32(48,0,true);assert.throws(() => decodePoseFrame(packet,1));
console.log('PASS pose protocol: precision, endian, mapping, truncation and invalid values');

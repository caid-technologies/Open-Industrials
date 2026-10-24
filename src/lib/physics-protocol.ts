export type PoseFrame = { step: number; time: number; poses: Float32Array };
export type PhysicsManifest = {
  schema: 'oi-physics-bundle/v1'; frame: string; timestep: number;
  sourceSha256: string; configSha256: string;
  instances: { instanceId: string; name: string; bodyId: number; bodyName: string; visualUri: string; mode: string }[];
  controls: { id: string; instanceId: string; range: [number, number]; unit: string }[];
};

export function decodePoseFrame(buffer: ArrayBuffer, expectedCount: number): PoseFrame {
  const view = new DataView(buffer);
  if (buffer.byteLength < 24 || view.getUint32(0, true) !== 0x3150494f || view.getUint32(4, true) !== 1) throw new Error('Unsupported physics pose packet');
  const count = view.getUint32(12, true), time = view.getFloat64(16, true);
  if (count !== expectedCount || buffer.byteLength !== 24 + count * 28 || !Number.isFinite(time) || time < 0) throw new Error('Invalid physics pose packet');
  const poses = new Float32Array(count * 7);
  for (let i = 0; i < poses.length; i++) {
    poses[i] = view.getFloat32(24 + i * 4, true);
    if (!Number.isFinite(poses[i])) throw new Error('Nonfinite physics pose');
  }
  for (let i = 0; i < count; i++) {
    const norm = Math.hypot(...poses.slice(i * 7 + 3, i * 7 + 7));
    if (Math.abs(norm - 1) > .001) throw new Error('Invalid pose quaternion');
  }
  return { step: view.getUint32(8, true), time, poses };
}

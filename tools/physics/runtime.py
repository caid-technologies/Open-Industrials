"""Transactional Float64 MuJoCo history; Float32 poses are display-only."""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import struct
from pathlib import Path

import mujoco
import numpy as np

SIGNATURE = int(mujoco.mjtState.mjSTATE_INTEGRATION)
MAX_STEPS = 1_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Conflict(ValueError):
    pass


class Runtime:
    def __init__(self, bundle: Path, database: Path):
        self.bundle = bundle.resolve()
        raw = (self.bundle / "manifest.json").read_bytes()
        self.manifest = json.loads(raw)
        if self.manifest["schema"] != "oi-physics-bundle/v1":
            raise ValueError("Unsupported bundle")
        for name, expected in self.manifest["checksums"].items():
            path = (self.bundle / name).resolve()
            if not path.is_relative_to(self.bundle) or sha(path.read_bytes()) != expected:
                raise ValueError(f"Bundle integrity failure: {name}")
        if self.manifest["engine"]["mujoco"] != mujoco.__version__:
            raise ValueError("Use the bundle's exact MuJoCo version")
        self.model = mujoco.MjModel.from_xml_path(str(self.bundle / "model.xml"))
        self.data = mujoco.MjData(self.model)
        self.size = mujoco.mj_stateSize(self.model, SIGNATURE)
        self.body_ids = [x["bodyId"] for x in self.manifest["instances"]]
        for instance in self.manifest["instances"]:
            if mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, instance["bodyName"]) != instance["bodyId"]:
                raise ValueError("Body mapping does not match model")
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS head (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, step INTEGER NOT NULL, running INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS frames (step INTEGER PRIMARY KEY, time REAL NOT NULL, state BLOB NOT NULL, state_hash TEXT NOT NULL, poses BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT NOT NULL, revision INTEGER UNIQUE NOT NULL, step INTEGER NOT NULL, controls BLOB NOT NULL);
        """)
        # Full integration state includes warmstarts, controls and applied forces.
        fingerprint = canonical({"bundle": sha(raw), "mujoco": mujoco.__version__, "numpy": np.__version__, "machine": platform.machine(), "system": platform.system(), "stateSignature": SIGNATURE, "stateSize": self.size})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            stored = self.db.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
            if stored and stored[0] != fingerprint:
                raise ValueError("Database model/engine/platform differs; replay requires the recorded environment")
            if not stored:
                self.db.execute("INSERT INTO metadata VALUES ('fingerprint',?)", (fingerprint,))
                self.db.execute("INSERT INTO head VALUES (1,0,0,0)")
                mujoco.mj_forward(self.model, self.data)
                self._record(0)
            self._restore_head()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _state(self):
        state = np.empty(self.size, dtype=np.float64)
        mujoco.mj_getState(self.model, self.data, state, SIGNATURE)
        if not np.isfinite(state).all():
            raise ValueError("Nonfinite physics state")
        return state.astype("<f8", copy=False).tobytes()

    def _restore(self, state):
        values = np.frombuffer(state, dtype="<f8").copy()
        if len(values) != self.size or not np.isfinite(values).all():
            raise ValueError("Invalid recorded physics state")
        mujoco.mj_setState(self.model, self.data, values, SIGNATURE)
        mujoco.mj_forward(self.model, self.data)
        # Forward recomputes derived positions; restoring again preserves the exact
        # saved warmstart/integration inputs for the next mj_step.
        mujoco.mj_setState(self.model, self.data, values, SIGNATURE)

    def _restore_head(self):
        revision, step, running = self.db.execute("SELECT revision,step,running FROM head WHERE id=1").fetchone()
        state, expected = self.db.execute("SELECT state,state_hash FROM frames WHERE step=?", (step,)).fetchone()
        if sha(state) != expected:
            raise ValueError("Recorded state checksum mismatch")
        self._restore(state)
        # Controls can change while paused at a recorded step. The last receipt
        # owns the control vector until the next step records it in its state.
        control = self.db.execute("SELECT controls FROM commands WHERE step=? ORDER BY revision DESC LIMIT 1", (step,)).fetchone()
        if control:
            self.data.ctrl[:] = np.frombuffer(control[0], dtype="<f8")
        return revision, step, bool(running)

    def _poses(self):
        # Read body frames, not recentered mesh/geom frames. Browser expects xyzw.
        values = np.empty((len(self.body_ids), 7), dtype="<f4")
        for i, body in enumerate(self.body_ids):
            values[i, :3] = self.data.xpos[body]
            q = self.data.xquat[body]
            values[i, 3:] = [q[1], q[2], q[3], q[0]]
        if not np.isfinite(values).all():
            raise ValueError("Pose overflow")
        return values.tobytes()

    def _record(self, step):
        state = self._state()
        # Derived transforms after mj_step may lag the final integrated qpos.
        self._restore(state)
        self.db.execute("INSERT INTO frames VALUES (?,?,?,?,?)", (step, float(self.data.time), state, sha(state), self._poses()))

    def _advance(self, start, steps):
        if start + steps > MAX_STEPS:
            raise ValueError("Recording limit reached; create a new database")
        for step in range(start + 1, start + steps + 1):
            before = float(self.data.time)
            mujoco.mj_step(self.model, self.data)
            if not np.isclose(self.data.time, before + self.model.opt.timestep, rtol=0, atol=1e-9):
                raise ValueError("Engine reset or invalid time progression")
            self._record(step)
        self.db.execute("UPDATE head SET step=? WHERE id=1", (start + steps,))

    def status(self):
        revision, step, running = self._restore_head()
        return {"revision": revision, "step": step, "running": running, "time": float(self.data.time), "controls": self.data.ctrl.tolist(), "maxSteps": MAX_STEPS}

    def command(self, request):
        if not isinstance(request, dict) or set(request) - {"id", "expectedRevision", "action", "values", "steps", "label"}:
            raise ValueError("Unknown command fields")
        if not isinstance(request.get("id"), str) or not 1 <= len(request["id"]) <= 128 or type(request.get("expectedRevision")) is not int:
            raise ValueError("Command requires id and expectedRevision")
        action = request.get("action")
        allowed = {"id", "expectedRevision", "action", "label"} | ({"values"} if action == "control" else {"steps"} if action == "step" else set())
        if action not in ("play", "pause", "control", "step") or set(request) - allowed:
            raise ValueError("Unsupported action or fields")
        if "label" in request and (not isinstance(request["label"], str) or len(request["label"]) > 160):
            raise ValueError("Invalid workflow label")
        encoded = canonical(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT request,receipt FROM commands WHERE id=?", (request["id"],)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise Conflict("Command ID already belongs to another request")
                self.db.execute("COMMIT")
                return json.loads(existing[1])
            revision, step, running = self._restore_head()
            if revision != request["expectedRevision"]:
                raise Conflict("Revision changed; read status and reconcile")
            if action == "control":
                values = request.get("values")
                if not isinstance(values, list) or len(values) != self.model.nu or any(type(v) not in (int, float) or not np.isfinite(v) for v in values):
                    raise ValueError("Expected one finite control per actuator")
                if any(not lo <= value <= hi for value, (lo, hi) in zip(values, self.model.actuator_ctrlrange)):
                    raise ValueError("Control exceeds the declared actuator limit")
                self.data.ctrl[:] = values
            elif action == "step":
                steps = request.get("steps")
                if running or type(steps) is not int or not 1 <= steps <= 1000:
                    raise ValueError("Pause before stepping 1..1000 steps")
                self._advance(step, steps)
                step += steps
            else:
                running = action == "play"
                if running and step >= MAX_STEPS:
                    raise ValueError("Recording is full")
            receipt = {"id": request["id"], "revision": revision + 1, "step": step, "time": float(self.data.time), "running": running, "action": action}
            self.db.execute("UPDATE head SET revision=?,running=? WHERE id=1", (revision + 1, int(running)))
            self.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?)", (request["id"], encoded, canonical(receipt), revision + 1, step, self.data.ctrl.astype("<f8").tobytes()))
            self.db.execute("COMMIT")
            return receipt
        except BaseException:
            self.db.execute("ROLLBACK")
            self._restore_head()
            raise

    def tick(self, steps):
        if type(steps) is not int or not 1 <= steps <= 1000:
            raise ValueError("Invalid worker batch")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            _, step, running = self._restore_head()
            if running:
                count = min(steps, MAX_STEPS-step)
                self._advance(step, count)
                if step + count == MAX_STEPS:
                    self.db.execute("UPDATE head SET running=0 WHERE id=1")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self._restore_head()
            raise

    def packet(self, step=None):
        if step is None:
            step = self.db.execute("SELECT step FROM head WHERE id=1").fetchone()[0]
        row = self.db.execute("SELECT time,poses FROM frames WHERE step=?", (step,)).fetchone()
        if not row:
            raise ValueError("Unknown recorded step")
        return struct.pack("<4sIIId", b"OIP1", 1, step, len(self.body_ids), row[0]) + row[1]

    def history(self, after=0, limit=50):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history page")
        rows = self.db.execute("SELECT request,receipt FROM commands WHERE revision>? ORDER BY revision LIMIT ?", (after, limit)).fetchall()
        return [{"request": json.loads(r), "receipt": json.loads(s)} for r, s in rows]

    def replay(self, start=0, end=None):
        """Re-simulate committed controls, comparing every Float64 integration byte.

        Runs on independent MjData and never edits live state or history. Checkpoints
        and controls must be applied at their original simulation step boundary.
        """
        self.db.execute("BEGIN")
        original = self.data
        try:
            last = self.db.execute("SELECT step FROM head WHERE id=1").fetchone()[0]
            end = last if end is None else end
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= last or end-start > 10_000:
                raise ValueError("Replay requires a recorded range of at most 10000 steps")
            row = self.db.execute("SELECT state,state_hash FROM frames WHERE step=?", (start,)).fetchone()
            if sha(row[0]) != row[1]:
                raise ValueError("Corrupt starting state")
            self.data = mujoco.MjData(self.model)
            self._restore(row[0])
            for step in range(start, end):
                control = self.db.execute("SELECT controls FROM commands WHERE step=? ORDER BY revision DESC LIMIT 1", (step,)).fetchone()
                if control:
                    self.data.ctrl[:] = np.frombuffer(control[0], dtype="<f8")
                mujoco.mj_step(self.model, self.data)
                state = self._state()
                recorded, expected = self.db.execute("SELECT state,state_hash FROM frames WHERE step=?", (step+1,)).fetchone()
                if sha(recorded) != expected or sha(state) != expected:
                    return {"passed": False, "divergentStep": step+1, "start": start, "end": end, "comparison": "Float64 integration bytes"}
                self._restore(state)
            return {"passed": True, "start": start, "end": end, "steps": end-start, "comparison": "Float64 integration bytes"}
        finally:
            self.data = original
            self.db.execute("ROLLBACK")

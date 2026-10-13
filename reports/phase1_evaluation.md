# Phase 1 Evaluation Report (Step 14)

Evidenced evaluation record. Every value below is labeled as one of:
**MEASURED** (actually executed locally, conditions given), **TEST
COVERAGE** (automated regression suite), or **NOT RUN / BLOCKED / NOT
MEASURED** (with the specific reason). Nothing is fabricated.

## 1. Scope and environment

| Field | Value |
|---|---|
| Evaluation date | 2026-10-13 |
| Project | AI-Powered Visual Attendance System, Phase 1, Step 14 |
| Python | 3.14.7 |
| Platform | Windows-10-10.0.19045 (Git Bash, local workstation) |
| numpy / opencv | 2.5.3 / 5.0.0 |
| Data used | Synthetic, non-biometric arrays only; production `data/` untouched |
| Camera | None at evaluation time (no live-capture cells were run) |
| Runner | `notebooks/09_phase1_evaluation.ipynb` (validated: all 11 code cells executed, exit 0) |

Latency numbers below are workstation- and load-dependent; treat them as
orders of magnitude, not specifications. Timing method (uniform across
measurements): `time.perf_counter`, 10–20 warm-up iterations, median of
100–500 timed iterations.

## 2. Artifact availability

| Artifact | Status | Impact |
|---|---|---|
| `models/yunet.onnx` | **PRESENT** (operator-supplied, local) | Detection software path can execute |
| `models/sface.onnx` | **MISSING** (not downloaded by policy) | All embedding-dependent empirical evaluation (enrollment end-to-end, recognition accuracy, FAR/FRR, unknown-person rates, camera→event latency) is **BLOCKED** |

## 3. Regression-test evidence (TEST COVERAGE)

Commands (exact):

```
# project root (all 654 passed, exit 0)
python -m pytest -q

# external working directory (established project requirement)
cd /tmp/empty-dir && <project>/.venv/Scripts/python.exe -m pytest <project>/tests -q
```

| Run | Result |
|---|---|
| Baseline before Step 14 changes | 654 passed, exit 0 |
| Full suite after Step 14 changes (project root) | 654 passed, exit 0 |
| Full suite after Step 14 changes (external CWD) | 654 passed, exit 0 |

The suite covers Steps 02–13 contracts deterministically (fake clocks,
synthetic vectors/crops; no camera, no model files, no network).

## 4. Face detection

| Item | Status | Evidence |
|---|---|---|
| Precision / recall vs annotated ground truth | **NOT RUN** | No ground-truth-annotated dataset exists in the repository; none was created or downloaded. Matching criterion is pre-defined: IoU ≥ 0.5 (helper verified: overlapping boxes IoU 0.4706, disjoint 0.0) |
| Artifact sanity + inference on synthetic frame | **MEASURED** | `yunet.onnx` loads (`is_loaded=True`); uniform 240×320 BGR frame → 0 detections (expected); median latency 7.0–10.8 ms across repeated runs |
| Detection latency on real images | **NOT MEASURED** | Synthetic frames only; real-content timing requires labeled data |

## 5. Face quality (synthetic thresholds, proxy pose)

**MEASURED on deterministic synthetic frames** (policy thresholds from
`QUALITY`: sharpness ≥ 50, brightness ∈ [40, 220]; the pose check is a
documented landmark-offset-ratio **proxy**, not 3-D yaw/pitch, and was
skipped here because synthetic detections carry no landmarks):

| Synthetic condition | Decision | First-failed reason | sharpness | brightness |
|---|---|---|---|---|
| uniform dark (mean 10) | reject | blur | 0.00 | 10.00 |
| uniform bright (mean 245) | reject | blur | 0.00 | 245.00 |
| uniform mid (mean 128) | reject | blur | 0.00 | 128.00 |
| checkerboard (mean 128) | accept | — | 2766.95 | 128.00 |

Note: the assessor reports the *first failing check* per face, so the
dark/bright rows show `blur` (sharpness 0 < 50) even though brightness
also failed; both conditions violate the window. Face-count policy
(single-face required): 0 faces → `no_faces`, 2 faces →
`multiple_faces`, 1 face → policy satisfied. **Real-lighting behavior
(glasses, masks, hats, occlusion, head rotation, distance) is NOT RUN
— no consented labeled images exist.**

## 6. Face recognition (software decisions, no accuracy claims)

**MEASURED on synthetic orthogonal unit vectors** with a scriptable
embedder and a private temporary storage directory (each record paired
with its Step 07 enrollment manifest, mirroring the eligibility rule):

| Query vs enrolled | Outcome |
|---|---|
| identical vector to enrolled `A` | `matched(A)`, best score 1.0 |
| orthogonal vector | `unknown`, best score 0.0 (< 0.36 threshold) |
| two collinear candidates (identical top scores, margin 0 < min_margin 0.1) | `ambiguous`, no identity assigned |

These rows verify the *decision procedure* only. FAR/FRR, unknown-person
rate, and threshold calibration: **NOT MEASURED** (missing `sface.onnx`;
no labeled dataset; no train/test separation to measure leakage against
— there is nothing to leak).

## 7. Temporal verification, unknown-face handling, attendance (software level)

All **MEASURED** with injected fake clocks (no sleeps):

* Temporal timeline (min_consistent=2): `collecting → verified["007"] →
  interrupted (by an uncertain frame) → collecting → verified["007"]` — the
  uncertain frame breaks the streak; IDs preserved exactly.
* Unknown-face handling: first unresolved frame `observed` → repeat
  `suppressed` → after exact cooldown (10.0 s) `observed` again
  (episode expired deterministically; verified identities end episodes).
* Check-in decisions: first `event_created` ("007", UTC-stamped
  `2026-10-13T09:00:00+00:00`); repeat within cooldown `suppressed`
  (no event exposed); exact boundary (`elapsed == 5.0 s`) → new event;
  different employee independent; inactive employee →
  `inactive_employee`, no event. Duplicate protection is engine-
  instance-lifetime only (no durable/cross-process exactly-once claim).

## 8. Performance summary

| What | Result | Label |
|---|---|---|
| YuNet detection, 240×320 uniform synthetic | median 7.0–10.8 ms | MEASURED (single repeated setup; machine-dependent) |
| `cosine_similarity` (4-D vectors) | median ~16–34 µs | MEASURED (software overhead only) |
| `assess_face` (112×112 synthetic crop) | median ~150–171 µs | MEASURED (synthetic only) |
| `TemporalVerifier.update` (fresh instance incl. construction) | median ~5 µs | MEASURED |
| SFace embedding inference | NOT MEASURED | artifact missing |
| End-to-end camera→event latency | NOT RUN | no camera at evaluation time (Step 15 scope) |

## 9. Blocked and unmeasured evaluations (with reasons)

| Evaluation | Status | Reason |
|---|---|---|
| Detection precision/recall (annotated GT) | NOT RUN | no annotated dataset; none created |
| Quality under real lighting/pose/occlusion | NOT RUN | no consented labeled images |
| Recognition accuracy (FAR/FRR/unknown rate) | NOT MEASURED | `sface.onnx` missing; no labeled dataset |
| Threshold calibration separation (train vs final eval) | NOT APPLICABLE YET | no calibration data exists at all |
| Lateness/attendance business-rule correctness | NOT IN SCOPE | Phase 2 (event engine implements check-in policy only) |
| Spoof resistance (print/photo/video replay) | NOT RUN | no liveness component in Phase 1; no authorized spoof rig; **no spoof-resistance claim is made** |
| Multi-camera | NOT RUN | no tracking across cameras exists (Step 12 documents session-key semantics) |

Documented future scenario plan (needs consented data + authorization
before ANY run): normal/low/bright lighting; glasses, masks, hats,
partial occlusion; head rotation and camera distances; multiple faces
and camera movement; unknown people; printed-photo, phone-photo, and
video-replay attempts.

## 10. Failure cases and unresolved risks

1. **Empirical CV evaluation is still fully blocked** on the missing
   `sface.onnx` artifact and any labeled dataset — the system's
   recognition accuracy is *unknown*, not merely unproven-large.
2. Quality poses are proxy ratios; they must not be sold as head-pose
   measurement.
3. Duplicate suppression is in-memory only; a restart can emit a
   second same-day check-in event; Phase 2 durable storage is planned.
4. Windows path conventions and `opencv` new-graph-engine warnings
   (`Targets are not supported ... for now` from DNN backend) were
   observed during the YuNet run; functional behavior was correct in
   this environment, but the warning should be re-checked on the
   target deployment platform.

## 11. Reproduction instructions

1. From the project root: `python -m pytest -q` (regression coverage).
2. Open `notebooks/09_phase1_evaluation.ipynb` and run all cells. The
   YuNet smoke cell is explicitly opt-in for the local artifact; the
   full-suite cell is opt-in (`RUN_FULL_TEST_SUITE = True`). Remote-CWD
   suite command is documented inside the notebook.
3. Expected blockers when `sface.onnx` is absent: none — all cells
   degrade to `BLOCKED`/synthetic messages; nothing is downloaded.
4. Do not run with real biometric data mounted at `data/` while sharing
   outputs; the notebook itself never reads `data/`.

## 12. Status summary

* **Software regression status:** full suite green (654 tests at
  baseline; see §8 rerun after Step 14 changes).
* **Empirical CV evaluation status:** BLOCKED (missing artifact/data),
  by design of this offline evaluation step — not a defect of the
  implementation.
* Phase 1 remains camera-integration incomplete by design until
  Step 15.

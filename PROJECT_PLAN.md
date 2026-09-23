# PROJECT_PLAN.md

Roadmap and intended architecture for the AI-Powered Visual Attendance
System. This file describes the *plan*; for what is actually implemented,
see [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md).

## Development philosophy

```text
ONE STEP → IMPLEMENT → RUN → VERIFY → FIX → DOCUMENT → COMMIT → APPROVAL → NEXT STEP
```

Rules:

1. One step at a time — never implement multiple roadmap steps together.
2. Inspect before modifying.
3. Do not guess: stop and ask when an architectural decision is unclear.
4. Preserve working code.
5. No hidden changes: report every file created or modified.
6. Test every step with an explicit verification procedure.
7. Documentation follows implementation — never claim unimplemented work.
8. Local commits only; the repository owner alone pushes to GitHub.

## Core architectural principle

Identity, Recognition, and Attendance are three separate components:

* **Identity** — who is this person? (enrollment, face samples, embeddings)
* **Recognition** — how confident are we that a detected face belongs to
  that person?
* **Attendance** — should this recognition create an attendance event?

Pipeline:

```text
Camera → Face Detection → Face Quality → Recognition → Confidence
       → Temporal Verification → Attendance Decision → Attendance Event
```

A face being recognized does NOT automatically mean attendance is recorded.

## Phase 1 — Computer Vision Core

```text
01. Project structure & environment            ✅ complete
02. Configuration & constants                  ⬜ not started
03. Employee data model                        ⬜
04. Live camera service                        ⬜
05. Face detection                             ⬜
06. Face quality assessment                    ⬜
07. Multi-image face enrollment                ⬜
08. Face embedding generation                  ⬜
09. Embedding storage                          ⬜
10. Face recognition                           ⬜
11. Temporal / multi-frame verification        ⬜
12. Unknown-face handling                      ⬜
13. Attendance event engine                    ⬜
14. Testing & evaluation                       ⬜
15. Phase 1 integration demo                   ⬜
```

(Completion markers are updated only after a step is verified. See
DEVELOPMENT_STATUS.md for details.)

## Later phases (blocked until Phase 1 is stable)

```text
PHASE 2 → Attendance/business rules
PHASE 3 → PostgreSQL + FastAPI
PHASE 4 → Web dashboard
PHASE 5 → Security / RBAC / audit
PHASE 6 → Docker / deployment
PHASE 7 → Advanced liveness + optimization
```

These phases are intentionally out of scope until Phase 1 is complete.

## Intended repository structure

```text
visual-attendance/
├── README.md  PROJECT_PLAN.md  DEVELOPMENT_STATUS.md
├── ARCHITECTURE.md  CHANGELOG.md  LICENSE
├── requirements.txt  .env.example  .gitignore
├── notebooks/            # experimentation & demonstration
├── src/                  # reusable production code
│   ├── config.py         # Step 02
│   ├── camera.py         # Step 04
│   ├── detection.py      # Step 05
│   ├── quality.py        # Step 06
│   ├── enrollment.py     # Step 07
│   ├── embeddings.py     # Step 08
│   ├── recognition.py    # Step 10
│   ├── verification.py   # Step 11
│   └── attendance.py     # Step 13
├── data/                 # employees, face_samples, embeddings, logs
├── models/  tests/  reports/  docs/
```

Stable logic moves gradually from notebooks into `src/`.

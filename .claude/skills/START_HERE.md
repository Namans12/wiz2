# START_HERE — wizlight-build skill

This folder is a self-bootstrapping architecture for working on WizLight in
Claude Code. To resume work in a fresh session, paste the prompt below.

## Starter prompt (paste into a new session)

> Load the wizlight-build skill before doing anything. In order:
> 1. Read `.claude/skills/SKILL.md` completely.
> 2. Read `.claude/skills/AGENT.md`.
> 3. Read `.claude/skills/MEMORY.md` and load `.claude/skills/state/state.json`.
> 4. Then tell me the last artifact built, any open follow-ups (e.g. release
>    signing), and wait for my task. Follow the SKILL.md workflow and write a
>    checkpoint to `.claude/skills/state/checkpoint.log` after each step.

## What each file controls
- **SKILL.md** — the what/how: scope, environment, toolchain, the 10-step build→verify→package workflow, output rules, known failure modes.
- **AGENT.md** — autonomy rules: allowed tools, the checkpoint loop, retry rules, escalation triggers, and hard "do nots".
- **MEMORY.md** — persistence: state schema, how to read/update it, and the durable facts to remember across sessions (device profiles, signing status, macOS-host constraint).
- **CHECKS.md** — the gate: structure/parity/behavior/state checks that must pass before any build is reported done.
- **state/state.json** — current state (last artifact, history, flags).
- **state/checkpoint.log** — per-step OK/FAILED trail for the most recent run.

## Note on environment
These files are written for **this** environment: Windows + Claude Code, real
tools (Read/Write/Edit/Bash/PowerShell), real repo paths. They intentionally do
NOT use the Linux code-interpreter conventions (`/mnt/user-data`, `present_files`,
`--break-system-packages`) from the generic template — those don't exist here.

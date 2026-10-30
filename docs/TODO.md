# TODO

- [ ] **Other machines:** check whether a resumed session keeps its `bridge:session_…` address. If it
  does, runbook step 4 in `claude/README.md` can go.
  Evidence so far: `training-kisski` restarted (2026-10-30, moved login node) and kept its address.
- [ ] **Optional, auto-boot:** run `sm.py boot` from the job script so no manual step is needed after a
  restart, e.g. `(sleep 60; python3 <repo>/claude/sm.py boot) >> ~/sm_boot.log 2>&1 &`.
  Untested: `claude --bg` from a batch shell with no terminal.

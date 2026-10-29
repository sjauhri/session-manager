# TODO

- [ ] **Remote peers:** check whether a resumed session keeps its `bridge:session_…` address. If it
  does, the runbook's re-announce step (`claude/README.md`, step 4) can go.
- [ ] **Optional — auto-boot:** run `sm.py boot` from the job script, before it blocks, so no manual step is
  needed after a restart, e.g. `(sleep 60; python3 <repo>/claude/sm.py boot) >> ~/sm_boot.log 2>&1 &`.
  Untested: `claude --bg` from a batch shell with no TTY.

Tank Exporter PY -- requirements folder
=======================================

This folder holds the dependency manifest go.bat consumes on first
install.  The `requirements.txt` next to this file is enough on its
own (pip will pull each package from PyPI); if you want a fully
offline-capable installer, run

    python -m pip download -r requirements.txt -d .

inside this folder before zipping the project.  go.bat will pick up
any *.whl files in here automatically when it sees them
(`pip install --find-links .` mode).

Lifecycle
---------

* First go.bat run with this folder present:
    1. Copy this folder verbatim to `<project>/resources/requirements_backup/`
       (permanent backup -- never deleted).
    2. `pip install -r requirements.txt`.
    3. On success, DELETE this folder so the project tree is clean.

* Subsequent go.bat runs see no `requirements/` folder, so they skip
  the install step entirely and go straight to launching the viewer.

* uninstall.bat removes the installed packages.
* reinstall.bat copies `resources/requirements_backup/` back to
  `requirements/`, then force-reinstalls every package.

DO NOT delete `resources/requirements_backup/` by hand.  That folder
is the only on-disk record of what we depend on once the live
`requirements/` folder has been consumed.

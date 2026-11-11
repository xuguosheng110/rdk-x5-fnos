# Public source regression tests

Run `sh scripts/test-public-release.sh` on Linux with Python 3. These tests do not access a live board or write a physical disk. They validate the portable product integration and simulated lab behavior, not fnOS end-to-end acceptance; see TESTED.md for board evidence.

The full historical `unittest discover` suite is retained but is not self-contained: boot_r3, hardware_backends, ram_deploy, remote_exec, runtime_diagnostics and handoff_scripts reference private historical receipts, local CP5 skill installation or host shell tooling. These records are intentionally not published as authorizations. Three experimental semantic test modules require NumPy and are outside this command. No full-suite-pass claim is made. Board bindings ship unreviewed and must be configured for each lab.

Release export verification (2026-11-11): 163 tests run, 162 passed, 1 skipped because the dedicated X5 ONNX toolchain is unavailable in this interpreter. Full image split/join round trip SHA256 verified.

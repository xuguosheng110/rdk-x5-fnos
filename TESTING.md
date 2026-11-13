# Public source regression tests

Run `sh scripts/test-public-release.sh` on Linux with Python 3. These tests do not access a live board or write a physical disk. They validate the portable product integration and simulated lab behavior, not fnOS end-to-end acceptance; see TESTED.md for board evidence.

The full historical `unittest discover` suite is retained but is not self-contained: boot_r3, hardware_backends, ram_deploy, remote_exec, runtime_diagnostics and handoff_scripts reference private historical receipts, local CP5 skill installation or host shell tooling. These records are intentionally not published as authorizations. Three experimental semantic test modules require NumPy and are outside this command. No full-suite-pass claim is made. Board bindings ship unreviewed and must be configured for each lab.

v0.2.0 source verification (2026-11-11): 163 tests run, 162 passed, 1 skipped because the dedicated X5 ONNX toolchain was unavailable in the test interpreter. The assembly checks and the llbitmap timeout simulation are recorded in [TESTED.md](TESTED.md). The image split/join verification applies to v0.1.0.


v0.3.0 source draft (2026-11-12): the staging tree is assembled from the
public v0.2.0 source commit plus the r20/r6 accepted source map. No new
full-suite pass is claimed for this draft. The 49 checks are finished-image
checks on the build server, and the r6 native-upgrade result is restricted to
the already configured target. Final source regression, new-board install and
real-board acceptance must be run after the draft is reviewed and published
inputs are sealed.

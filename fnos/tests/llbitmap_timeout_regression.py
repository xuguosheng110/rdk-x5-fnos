"""Compile the real source function with a simulated timeout; no kernel/runtime acceptance claim."""
import subprocess
import sys
import tempfile
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit(f"Usage: {sys.argv[0]} path/to/drivers/md/md-llbitmap.c")

source = Path(sys.argv[1]).read_text()
start = source.index("static int llbitmap_suspend_timeout(")
body_start = source.index("{", start)
depth = 1
for end, char in enumerate(source[body_start + 1 :], body_start + 1):
    if char == "{":
        depth += 1
    elif char == "}":
        depth -= 1
        if depth == 0:
            break
else:
    raise ValueError("Incomplete llbitmap_suspend_timeout function body")
function = source[start : end + 1]

stubs = '''#include <errno.h>
#include <stdio.h>
struct llbitmap_page_ctl { int active; int wait; };
struct mddev { struct { int daemon_sleep; } bitmap_info; };
struct llbitmap { struct llbitmap_page_ctl **pctl; struct mddev *mddev; };
static int result;
static void percpu_ref_kill(int *p) { *p = 0; }
static void percpu_ref_resurrect(int *p) { *p = 1; }
#define wait_event_timeout(a,b,c) (result)
#define HZ 100
'''
main = '''
int main(void) {
    struct llbitmap_page_ctl page = {.active = 1};
    struct llbitmap_page_ctl *pages[] = {&page};
    struct mddev md = {.bitmap_info = {.daemon_sleep = 30}};
    struct llbitmap bitmap = {.pctl = pages, .mddev = &md};

    result = 0;
    int rc = llbitmap_suspend_timeout(&bitmap, 0);
    if (rc != -ETIMEDOUT || page.active != 1) {
        fprintf(stderr, "FAIL: timeout leaves reference killed\\n");
        return 1;
    }

    result = 1;
    rc = llbitmap_suspend_timeout(&bitmap, 0);
    if (rc != 0 || page.active != 0) {
        fprintf(stderr, "FAIL: success path altered\\n");
        return 2;
    }
    puts("PASS: timeout restores reference; success remains suspended");
    return 0;
}
'''

with tempfile.TemporaryDirectory() as temporary:
    test_source = Path(temporary) / "test.c"
    executable = Path(temporary) / "test"
    test_source.write_text(stubs + function + main)
    subprocess.run(
        ["cc", "-std=c11", "-Wall", str(test_source), "-o", str(executable)],
        check=True,
    )
    sys.exit(subprocess.run([str(executable)], check=False).returncode)

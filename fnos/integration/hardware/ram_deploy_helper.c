/* RAM-only SD writer. Production has no regular-file or fault-injection mode.
 * SHA-256 is delegated to the pinned rescue BusyBox, never implemented here.
 * Build on build-server: aarch64-linux-gnu-gcc -static -O2 -Wall -Wextra -Werror
 *                    ram_deploy_helper.c -o ram_deploy_helper
 */
#define _GNU_SOURCE
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/sysmacros.h>
#include <sys/ioctl.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <linux/fs.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>

#define CHUNK (1024 * 1024)
#define SD "/dev/mmcblk1"
#define SYS "/sys/block/mmcblk1"
#define LOCK "/run/fnos-ram-deploy.lock"
static int lockfd = -1, targetfd = -1;
static char token[65], expected_boot[64];
static volatile sig_atomic_t interrupted;
static uint64_t written, readback;
static int hashpid = -1;
static const char *state = "initializing";

static const char *mapped(const char *path) {
#ifdef RAM_DEPLOY_TEST
    static char result[PATH_MAX];
    const char *root = getenv("RAM_DEPLOY_TEST_ROOT");
    if (!root || snprintf(result, sizeof result, "%s%s", root, path) >= (int)sizeof result) exit(97);
    return result;
#else
    return path;
#endif
}
static int fault(const char *name) {
#ifdef RAM_DEPLOY_TEST
    const char *value = getenv("RAM_DEPLOY_TEST_FAULT");
    return value && !strcmp(value, name);
#else
    (void)name;
    return 0;
#endif
}
static void persist(void) {
    if (lockfd < 0) return;
    char row[512];
    int n = snprintf(row, sizeof row, "{\"schema\":\"fnos-ram-writer-state/v1\",\"token\":\"%s\",\"pid\":%ld,\"status\":\"%s\",\"written\":%llu,\"readback\":%llu}\n",
        token, (long)getpid(), state, (unsigned long long)written, (unsigned long long)readback);
    if (n > 0 && n < (int)sizeof row) {
        if (ftruncate(lockfd, 0) || pwrite(lockfd, row, (size_t)n, 0) != n || fsync(lockfd)) {
            fprintf(stderr, "run evidence write failed; completion is NOT verified\n");
            if (hashpid > 0) { kill(hashpid, SIGKILL); waitpid(hashpid, NULL, 0); }
            exit(2);
        }
    }
}
static void die(const char *reason) {
    state = "FAILED"; persist();
    fprintf(stderr, "REFUSED: %s (errno=%d)\n", reason, errno);
    if (hashpid > 0) { kill(hashpid, SIGKILL); waitpid(hashpid, NULL, 0); }
    if (targetfd >= 0) close(targetfd);
    exit(1);
}
static void on_signal(int sig) { interrupted = sig; }
static void check_signal(void) { if (interrupted) die("interrupted; no reboot allowed"); }
static void read_text(const char *name, char *out, size_t size) {
    int fd = open(mapped(name), O_RDONLY | O_CLOEXEC);
    if (fd < 0) die("required kernel evidence unavailable");
    ssize_t n = read(fd, out, size - 1);
    if (n < 0 || (size_t)n == size - 1) { close(fd); die("invalid/oversized kernel evidence"); }
    close(fd); out[n] = 0;
    while (n && (out[n-1] == '\n' || out[n-1] == '\r')) out[--n] = 0;
}
static uint64_t number(const char *text) {
    char *end; errno = 0;
    if (!text[0] || text[0] == '-') die("invalid unsigned size");
    unsigned long long n = strtoull(text, &end, 10);
    if (errno || *end || !n) die("invalid unsigned size");
    return (uint64_t)n;
}
static int hexadecimal(const char *s, size_t n) {
    if (strlen(s) != n) return 0;
    for (size_t i = 0; i < n; i++) if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return 0;
    return 1;
}
static int empty_directory(const char *path) {
    DIR *dir = opendir(mapped(path));
    if (!dir) die("holders evidence unavailable");
    struct dirent *entry; int empty = 1;
    while ((entry = readdir(dir))) if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) empty = 0;
    closedir(dir); return empty;
}
static void inactive(void) {
    struct statfs fs;
#ifdef RAM_DEPLOY_TEST
    char root[32]; read_text("/root-type", root, sizeof root);
    if (strcmp(root, "ramfs")) die("root is not RAM");
    (void)fs;
#else
    if (statfs("/", &fs) || (fs.f_type != 0x858458f6 && fs.f_type != 0x01021994)) die("root is not RAM");
#endif
    char boot[64]; read_text("/proc/sys/kernel/random/boot_id", boot, sizeof boot);
    if (strcmp(boot, expected_boot)) die("rescue boot identity changed");
    FILE *f = fopen(mapped("/proc/self/mountinfo"), "r");
    if (!f) die("mount evidence unavailable");
    char *line = NULL; size_t size = 0; unsigned a, b; int count = 0;
    while (getline(&line, &size, f) > 0) {
        if (sscanf(line, "%*u %*u %u:%u", &a, &b) != 2) die("invalid mountinfo");
        if (a != 0) die("persistent block filesystem mounted");
        count++;
    }
    if (ferror(f) || !count) die("incomplete mount evidence");
    fclose(f); free(line); line = NULL; size = 0;
    f = fopen(mapped("/proc/swaps"), "r");
    if (!f || getline(&line, &size, f) <= 0 || strncmp(line, "Filename", 8)) die("swap evidence unavailable");
    while (getline(&line, &size, f) > 0) if (strspn(line, " \t\r\n") != strlen(line)) die("swap is active");
    if (ferror(f)) die("swap evidence incomplete");
    fclose(f); free(line);
    if (!empty_directory(SYS "/holders")) die("SD has active holders");
    DIR *dir = opendir(mapped(SYS));
    if (!dir) die("SD sysfs absent");
    struct dirent *entry;
    while ((entry = readdir(dir))) if (!strncmp(entry->d_name, "mmcblk1p", 8)) {
        char path[PATH_MAX];
        snprintf(path, sizeof path, SYS "/%s/holders", entry->d_name);
        if (!empty_directory(path)) die("SD partition has active holders");
    }
    closedir(dir);
}
static dev_t identity(const char *cid, uint64_t bytes, unsigned maj, unsigned min, int after) {
    char value[256]; unsigned a, b; char trailing;
    read_text(SYS "/device/cid", value, sizeof value);
    if (strcmp(value, cid) || (after && fault("cid_race"))) die("SD CID mismatch");
    read_text(SYS "/size", value, sizeof value);
    uint64_t sectors = number(value);
    if (sectors > UINT64_MAX/512 || sectors*512 != bytes) die("SD capacity mismatch");
    read_text(SYS "/dev", value, sizeof value);
    if (sscanf(value, "%u:%u%c", &a, &b, &trailing) != 2 || a != 179 || a != maj || b != min || (after && fault("identity_race"))) die("SD major/minor mismatch");
    dev_t device = makedev(a, b);
    struct stat st;
    if (lstat(mapped(SD), &st)) die("SD device unavailable");
#ifdef RAM_DEPLOY_TEST
    if (!S_ISREG(st.st_mode)) die("test target is not a regular fixture");
#else
    if (!S_ISBLK(st.st_mode) || st.st_rdev != device) die("target is not matching SD block node");
#endif
    if (after && fault("mount_after_open")) die("persistent block filesystem mounted");
    inactive();
    return device;
}
static void bind_fd(dev_t dev, uint64_t bytes) {
    struct stat st;
    if (fstat(targetfd, &st)) die("target fstat failed");
#ifdef RAM_DEPLOY_TEST
    if (!S_ISREG(st.st_mode) || (uint64_t)st.st_size != bytes || fault("capacity_race")) die("opened fixture capacity changed");
    (void)dev;
#else
    uint64_t actual = 0; int sector = 0;
    if (!S_ISBLK(st.st_mode) || st.st_rdev != dev || ioctl(targetfd, BLKGETSIZE64, &actual) || actual != bytes ||
        ioctl(targetfd, BLKSSZGET, &sector) || sector != 512) die("opened SD identity/capacity changed");
#endif
}
struct hasher { int input, output; pid_t pid; };
static struct hasher hash_start(void) {
    int in[2], out[2];
    if (pipe2(in, O_CLOEXEC) || pipe2(out, O_CLOEXEC)) die("hash pipe failed");
    pid_t pid = fork();
    if (pid < 0) die("hash fork failed");
    if (!pid) {
        if (dup2(in[0], 0) < 0 || dup2(out[1], 1) < 0) _exit(126);
        close(in[0]); close(in[1]); close(out[0]); close(out[1]);
#ifdef RAM_DEPLOY_TEST
        execl("/usr/bin/sha256sum", "sha256sum", (char *)NULL);
#else
        execl("/bin/busybox", "busybox", "sha256sum", (char *)NULL);
#endif
        _exit(127);
    }
    close(in[0]); close(out[1]); hashpid = pid;
    return (struct hasher){in[1], out[0], pid};
}
static void hash_data(struct hasher h, const void *buf, size_t n) {
    const char *p = buf;
    while (n) {
        check_signal(); ssize_t r = write(h.input, p, n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) die("hash subprocess input failed");
        p += r; n -= (size_t)r;
    }
}
static void hash_end(struct hasher h, const char *expected) {
    close(h.input); char out[256]; size_t total = 0;
    for (;;) {
        ssize_t n = read(h.output, out + total, sizeof out - 1 - total);
        if (n < 0 && errno == EINTR) { check_signal(); continue; }
        if (n < 0 || total == sizeof out - 1) die("invalid hash subprocess output");
        if (!n) break;
        total += (size_t)n;
    }
    close(h.output); out[total] = 0; int status;
    if (waitpid(h.pid, &status, 0) != h.pid || !WIFEXITED(status) || WEXITSTATUS(status)) die("hash subprocess failed");
    hashpid = -1;
    if (total != 68 || memcmp(out, expected, 64) || strcmp(out + 64, "  -\n")) die("full SHA256 mismatch");
}
static ssize_t input_read(void *buf, size_t n) {
    struct pollfd pollfd = {0, POLLIN, 0};
    for (;;) {
        check_signal(); int rc = poll(&pollfd, 1, 120000);
        if (rc < 0 && errno == EINTR) continue;
        if (rc <= 0) die("image input timed out");
        ssize_t got = read(0, buf, n);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) die("image input error");
        return got;
    }
}
static void status_only(const char *wanted) {
    if (!hexadecimal(wanted, 32)) die("invalid status token");
    int fd = open(mapped(LOCK), O_RDWR | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) die("no remote run evidence; completion unknown");
    if (flock(fd, LOCK_EX | LOCK_NB)) {
        puts("{\"status\":\"ACTIVE\"}"); close(fd); return;
    }
    char data[1024]; ssize_t n = pread(fd, data, sizeof data-1, 0);
    if (n < 0 || n >= (ssize_t)sizeof data-1) die("invalid run evidence");
    data[n] = 0;
    char needle[80]; snprintf(needle, sizeof needle, "\"token\":\"%s\"", wanted);
    if (!strstr(data, needle)) die("different remote run token; completion unknown");
    printf("{\"status\":\"TERMINAL\",\"run\":%s}\n", data);
    close(fd);
}
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal); signal(SIGHUP, on_signal);
#ifndef RAM_DEPLOY_TEST
    if (geteuid() != 0) die("RAM rescue root identity required");
#endif
    if (argc == 3 && !strcmp(argv[1], "--status")) { status_only(argv[2]); return 0; }
    if (argc != 10 || strcmp(argv[1], "--execute")) {
        fprintf(stderr, "usage: helper --execute TOKEN BOOT_ID CID CARD_BYTES MAJOR MINOR IMAGE_BYTES SHA256\n"); return 2;
    }
    if (!hexadecimal(argv[2], 32) || strlen(argv[3]) != 36 || !hexadecimal(argv[4], 32) || !hexadecimal(argv[9], 64)) die("invalid exact identity/hash arguments");
    strcpy(token, argv[2]); strcpy(expected_boot, argv[3]);
    uint64_t bytes = number(argv[5]), maj64 = number(argv[6]), min64;
    char *end; errno = 0; min64 = strtoull(argv[7], &end, 10);
    if (errno || *end || !argv[7][0] || min64 > UINT_MAX || maj64 != 179) die("invalid MMC device number");
    uint64_t length = number(argv[8]);
    if (bytes != 31914983424ULL || strcmp(argv[4], "035344534b333247854bf6868b01a200") || length > bytes || length % 512) {
#ifdef RAM_DEPLOY_TEST
        if (length > bytes || length % 512) die("invalid fixture size");
#else
        die("not the reviewed physical SD");
#endif
    }
    inactive();
    lockfd = open(mapped(LOCK), O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat lst;
    if (lockfd < 0 || fstat(lockfd, &lst) || !S_ISREG(lst.st_mode) || lst.st_nlink != 1 ||
        lst.st_uid != geteuid() || flock(lockfd, LOCK_EX | LOCK_NB)) {
        lockfd = -1; die("another writer or invalid remote lock");
    }
    state = "CHECKING"; persist();
    dev_t dev = identity(argv[4], bytes, (unsigned)maj64, (unsigned)min64, 0);
    targetfd = open(mapped(SD), O_RDWR | O_EXCL | O_NOFOLLOW | O_CLOEXEC);
    if (targetfd < 0) die("exclusive SD open failed");
    bind_fd(dev, bytes);
    if (identity(argv[4], bytes, (unsigned)maj64, (unsigned)min64, 1) != dev) die("SD changed during open");
    state = "WRITING"; persist(); puts("{\"status\":\"READY\"}");
    unsigned char *buf = malloc(CHUNK); if (!buf) die("buffer allocation failed");
    struct hasher hash = hash_start();
    while (written < length) {
        size_t want = length-written > CHUNK ? CHUNK : (size_t)(length-written), have = 0;
        while (have < want) { ssize_t n = input_read(buf+have, want-have); if (!n) die("short image input"); have += (size_t)n; }
        check_signal();
        ssize_t n;
        do { n = pwrite(targetfd, buf, fault("short_write") ? want-1 : want, (off_t)written); } while (n < 0 && errno == EINTR && !interrupted);
        if (n != (ssize_t)want) die("short or failed SD write");
        hash_data(hash, buf, want); written += want;
        if (!(written % (64ULL*CHUNK))) { persist(); printf("{\"status\":\"WRITING\",\"bytes\":%llu}\n", (unsigned long long)written); }
    }
    if (input_read(buf, 1) != 0) die("extra bytes after exact image length");
    hash_end(hash, argv[9]);
    if (fsync(targetfd) || fault("fsync")) die("SD fsync failed");
#ifndef RAM_DEPLOY_TEST
    /* Discard the block-device cache after durable write so readback does not
     * merely hash our own cached writes. Retain this same opened block FD. */
    if (ioctl(targetfd, BLKFLSBUF, 0)) die("SD cache invalidation failed");
#endif
    if (fault("cache_flush")) die("SD cache invalidation failed");
    bind_fd(dev, bytes); identity(argv[4], bytes, (unsigned)maj64, (unsigned)min64, 1);
    state = "READBACK"; persist(); puts("{\"status\":\"READBACK\"}");
    hash = hash_start();
    while (readback < length) {
        check_signal(); size_t want = length-readback > CHUNK ? CHUNK : (size_t)(length-readback);
        ssize_t n;
        do { n = pread(targetfd, buf, fault("short_read") ? want-1 : want, (off_t)readback); } while (n < 0 && errno == EINTR && !interrupted);
        if (n != (ssize_t)want) die("short or failed SD readback");
        if (fault("readback_corruption") && !readback) buf[0] ^= 1;
        hash_data(hash, buf, want); readback += want;
        if (!(readback % (64ULL*CHUNK))) { persist(); printf("{\"status\":\"READBACK\",\"bytes\":%llu}\n", (unsigned long long)readback); }
    }
    hash_end(hash, argv[9]); bind_fd(dev, bytes);
    identity(argv[4], bytes, (unsigned)maj64, (unsigned)min64, 1);
    if (close(targetfd)) { targetfd = -1; die("SD close failed"); } targetfd = -1;
    state = "DONE"; persist();
    printf("{\"schema\":\"fnos-ram-deploy-result/v1\",\"status\":\"PASS\",\"token\":\"%s\",\"target\":\"%s\",\"written\":%llu,\"readback\":%llu,\"sha256\":\"%s\",\"automatic_reboot\":false}\n",
        token, SD, (unsigned long long)written, (unsigned long long)readback, argv[9]);
    close(lockfd); free(buf); return 0;
}

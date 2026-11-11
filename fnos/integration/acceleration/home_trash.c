/* SPDX-License-Identifier: MIT
 * fnOS ARM 1.2.0701 native recycle-bin compatibility for the home policy.
 * The native filemanager uses unlinkat(-7126, "volN/UID/...", flags).
 * Preserve its trash layout, collision suffix and user.trim-trashbin-record.
 * Ordinary unlinkat and all paths outside the selected data scope are intact.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#define TRIM_RECYCLE -7126
#define TRIM_PERMANENT -7125
#define RECORD "user.trim-trashbin-record"
extern int fnos_home_path(const char *, struct stat *);

/* Open every component without traversing symlinks; fd ownership is local. */
static int open_directory(const char *path, int create, const char *record) {
    char copy[PATH_MAX];
    if (strlen(path) >= sizeof(copy)) { errno = ENAMETOOLONG; return -1; }
    strcpy(copy, path);
    int fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return -1;
    char *save = NULL;
    for (char *p = strtok_r(copy, "/", &save); p; p = strtok_r(NULL, "/", &save)) {
        int made = 0;
        if (!strcmp(p, ".") || !strcmp(p, "..")) { close(fd); errno = EINVAL; return -1; }
        if (create) {
            if (!mkdirat(fd, p, 0777)) made = 1;
            else if (errno != EEXIST) { int e = errno; close(fd); errno = e; return -1; }
        }
        int next = openat(fd, p, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        int e = errno; close(fd); fd = next; errno = e;
        if (fd < 0) return -1;
        if (made && (fchmod(fd, 0777) ||
            (record && fsetxattr(fd, RECORD, record, strlen(record), 0)))) {
            e = errno; close(fd); errno = e; return -1;
        }
    }
    return fd;
}

static int digits(const char *start, const char *end) {
    if (start == end) return 0;
    for (; start != end; start++) if (*start < '0' || *start > '9') return 0;
    return 1;
}

static int recycle(const char *path, int flags) {
    char source[PATH_MAX], home[PATH_MAX], destination[PATH_MAX], record[96];
    struct stat st, opened, parent;
    int srcdir = -1, dst = -1, fd = -1, rc = -1, old_len = -1, marked = 0;
    void *old = NULL;
    if (flags & ~AT_REMOVEDIR) { errno = EINVAL; return -1; }
    /* Native special paths are rooted at /, regardless of process cwd. */
    int n = snprintf(source, sizeof(source), "%s%s", path[0] == '/' ? "" : "/", path);
    if (n < 0 || n >= PATH_MAX) { errno = ENAMETOOLONG; return -1; }
    if (strstr(source, "/../") || strstr(source, "/./") || strstr(source, "//") ||
        strstr(source, "/.@#local/") || !strcmp(source + strlen(source) - 1, "/")) {
        errno = EINVAL; return -1;
    }
    if (strncmp(source, "/vol", 4) || source[4] < '1' || source[4] > '9') { errno = EPERM; return -1; }
    char *volume_end = strchr(source + 4, '/');
    if (!volume_end || !digits(source + 4, volume_end)) { errno = EPERM; return -1; }
    char *home_end = strchr(volume_end + 1, '/');
    unsigned uid = getuid();
    if (!home_end) { errno = EPERM; return -1; }
    if (digits(volume_end + 1, home_end)) {
        uid = 0;
        for (const char *p = volume_end + 1; p < home_end; p++) {
            unsigned digit = (unsigned)(*p - '0');
            if (uid > (UINT_MAX - digit) / 10) { errno = EINVAL; return -1; }
            uid = uid * 10 + digit;
        }
    } else if (home_end - volume_end == 6 && !strncmp(volume_end + 1, "@team", 5)) {
        home_end = strchr(home_end + 1, '/');
        if (!home_end) { errno = EPERM; return -1; }
    } else { errno = EPERM; return -1; }
    if (!home_end[1] || !strcmp(home_end + 1, ".@#local")) { errno = EPERM; return -1; }
    if (!fnos_home_path(source, &st) || lstat(source, &opened) || S_ISLNK(opened.st_mode) ||
        st.st_dev != opened.st_dev || st.st_ino != opened.st_ino) { errno = EPERM; return -1; }
    if (S_ISDIR(st.st_mode) != !!(flags & AT_REMOVEDIR)) { errno = S_ISDIR(st.st_mode) ? EISDIR : ENOTDIR; return -1; }
    memcpy(home, source, home_end - source); home[home_end - source] = 0;
    n = snprintf(destination, sizeof(destination), "%s/.@#local/trash/%s", home, home_end + 1);
    if (n < 0 || n >= PATH_MAX) { errno = ENAMETOOLONG; return -1; }
    char *srcname = strrchr(source, '/'), *dstname = strrchr(destination, '/');
    *srcname++ = 0; *dstname++ = 0;
    if (!strcmp(srcname, ".") || !strcmp(srcname, "..")) { errno = EINVAL; return -1; }
    srcdir = open_directory(source, 0, NULL);
    if (srcdir < 0) goto done;
    fd = openat(srcdir, srcname, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0 || fstat(fd, &opened)) goto done;
    if (st.st_dev != opened.st_dev || st.st_ino != opened.st_ino) { errno = ESTALE; goto done; }
    snprintf(record, sizeof(record), "u:%u,t:%lu", uid, (unsigned long)time(NULL));
    dst = open_directory(destination, 1, record);
    if (dst < 0 || fstat(dst, &parent)) goto done;
    if (parent.st_dev != st.st_dev) { errno = EXDEV; goto done; }
    old_len = fgetxattr(fd, RECORD, NULL, 0);
    if (old_len < 0 && errno != ENODATA) goto done;
    if (old_len >= 0) {
        old = malloc(old_len ? (size_t)old_len : 1);
        if (!old || fgetxattr(fd, RECORD, old, old_len) != old_len) goto done;
    }
    if (fsetxattr(fd, RECORD, record, strlen(record), 0)) goto done;
    marked = 1;
    if (fsync(fd)) goto done;
    for (unsigned i = 0; i < 100000; i++) {
        char name[NAME_MAX + 1];
        const char *dot = strrchr(dstname, '.');
        if (!i) n = snprintf(name, sizeof(name), "%s", dstname);
        else if (dot && dot != dstname && dot[1])
            n = snprintf(name, sizeof(name), "%.*s_%u%s", (int)(dot - dstname), dstname, i, dot);
        else n = snprintf(name, sizeof(name), "%s_%u", dstname, i);
        if (n < 0 || n > NAME_MAX) { errno = ENAMETOOLONG; break; }
        rc = syscall(SYS_renameat2, srcdir, srcname, dst, name, RENAME_NOREPLACE);
        if (!rc || errno != EEXIST) break;
    }
    if (!rc) {
        /* The rename is committed. Never turn a successful move into a retry
         * that might affect another file; both directory updates are flushed. */
        (void)fsync(dst); (void)fsync(srcdir);
    }
done:;
    int e = errno;
    if (rc && marked) {
        if (old_len >= 0) (void)fsetxattr(fd, RECORD, old, old_len, 0);
        else (void)fremovexattr(fd, RECORD);
    }
    free(old);
    if (fd >= 0) close(fd);
    if (dst >= 0) close(dst);
    if (srcdir >= 0) close(srcdir);
    errno = e;
    return rc;
}

static int permanent(const char *path, int flags) {
    char full[PATH_MAX];
    struct stat parent;
    if (flags & ~AT_REMOVEDIR) { errno = EINVAL; return -1; }
    int n = snprintf(full, sizeof(full), "%s%s", path[0] == '/' ? "" : "/", path);
    if (n < 0 || n >= PATH_MAX) { errno = ENAMETOOLONG; return -1; }
    if (strncmp(full, "/vol", 4) || full[4] < '0' || full[4] > '9' ||
        strstr(full, "/../") || strstr(full, "/./") || strstr(full, "//")) {
        errno = EPERM; return -1;
    }
    char *volume_end = strchr(full + 4, '/'), *name = strrchr(full, '/');
    if (!volume_end || !digits(full + 4, volume_end) || name == volume_end ||
        !name[1] || !strcmp(name + 1, ".") || !strcmp(name + 1, "..")) { errno = EPERM; return -1; }
    *name++ = 0;
    /* Resolve the parent only: deleting a symlink removes the link, never its
     * target. No directory component is traversed through a symlink. */
    if (!fnos_home_path(full, &parent)) { errno = EPERM; return -1; }
    int fd = open_directory(full, 0, NULL);
    if (fd < 0) return -1;
    int rc = syscall(SYS_unlinkat, fd, name, flags), e = errno;
    if (!rc) (void)fsync(fd);
    close(fd); errno = e; return rc;
}

int unlinkat(int dfd, const char *path, int flags) {
    if (dfd == TRIM_RECYCLE && getenv("FNOS_HOME_DATA_ROOT")) return recycle(path, flags);
    if (dfd == TRIM_PERMANENT && getenv("FNOS_HOME_DATA_ROOT")) return permanent(path, flags);
    int (*next)(int, const char *, int) = dlsym(RTLD_NEXT, "unlinkat");
    if (!next) { errno = ENOSYS; return -1; }
    return next(dfd, path, flags);
}

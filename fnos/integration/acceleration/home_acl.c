/* SPDX-License-Identifier: MIT
 * Explicit single-user/home policy bridge for fnOS ARM 1.2.0701.
 * This is NOT the TrimACL kernel implementation. A configured home data tree
 * is shared read/write, as authorized by its owner. Native ACL reads expose
 * an everyone-allow entry; ACL writes retain that fixed home policy.
 * No global preload: enable only in the reviewed storage/application units.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

#define ALL_RIGHTS 0x7fcf
struct ace { uint16_t tag, reserved; uint32_t perm; uint16_t inherit, unused; uint32_t id, level; };
struct acl { uint32_t count; struct ace entry; };
struct get_acl { const char *filename; void *buffer; size_t size; int fd; unsigned flags; };
struct access_request { const char *filename; unsigned flags, uid, gid_count; unsigned *gids; };
struct realpath_request { const char *filename; char *resolved; };
_Static_assert(sizeof(struct ace) == 20 && sizeof(struct acl) == 24, "ARM ACL ABI");

static const char *root(void) {
    const char *p = getenv("FNOS_HOME_DATA_ROOT");
    /* Restrict the opt-in to a data volume, never the OS or credentials tree. */
    if (!p || strncmp(p, "/vol", 4) || p[4] < '1' || p[4] > '9') return NULL;
    const char *q = p + 5;
    while (*q >= '0' && *q <= '9') q++;
    if (*q == 0) return p;
    if (*q != '/' || !q[1] || strstr(q, "/../") || strstr(q, "/./") || p[strlen(p)-1] == '/') return NULL;
    return p;
}

static int in_home(const char *path, struct stat *info) {
    const char *setting = getenv("FNOS_HOME_DATA_ROOT");
    const char *r = NULL;
    char full[PATH_MAX], canonical_root[PATH_MAX];
    if (!setting || !path || !realpath(path, full)) return 0;
    if (!strcmp(setting, "/vol*") || !strcmp(setting, "auto")) {
        if (!strncmp(full, "/vol00/", 7) || !strncmp(full, "/vol01/", 7) ||
            !strncmp(full, "/vol02/", 7)) {
            /* Native external/remote namespaces. Only a mounted child is
             * data: the namespace directories themselves belong to the OS. */
            char walk[PATH_MAX], parent_path[PATH_MAX];
            struct stat current, parent, osroot;
            if (stat("/", &osroot)) return 0;
            strcpy(walk, full);
            canonical_root[0] = 0;
            while (strlen(walk) > 6) {
                strcpy(parent_path, walk);
                char *slash = strrchr(parent_path, '/');
                if (!slash) return 0;
                *slash = 0;
                if (stat(walk, &current) || stat(parent_path, &parent)) return 0;
                if (S_ISDIR(current.st_mode) && current.st_dev != parent.st_dev && current.st_dev != osroot.st_dev) {
                    strcpy(canonical_root, walk); break;
                }
                strcpy(walk, parent_path);
            }
            if (!canonical_root[0]) return 0;
            r = canonical_root;
        } else {
        /* Automatic policy follows mounted fnOS data volumes, never a stale
         * mountpoint directory on the operating-system filesystem. */
        if (strncmp(full, "/vol", 4) || full[4] < '1' || full[4] > '9') return 0;
        size_t end = 5;
        while (full[end] >= '0' && full[end] <= '9') end++;
        if (full[end] && full[end] != '/') return 0;
        memcpy(canonical_root, full, end); canonical_root[end] = 0;
        struct stat volume, osroot;
        if (stat(canonical_root, &volume) || stat("/", &osroot) || volume.st_dev == osroot.st_dev) return 0;
        r = canonical_root;
        }
    } else {
        r = root();
        if (!r || !realpath(r, canonical_root) || strcmp(r, canonical_root)) return 0;
    }
    size_t n = strlen(r);
    if (strncmp(full, r, n) || (full[n] && full[n] != '/')) return 0;
    if (stat(full, info) || (!S_ISREG(info->st_mode) && !S_ISDIR(info->st_mode))) return 0;
    return 1;
}

/* Shared scope predicate for the portable native recycle-bin adapter. */
__attribute__((visibility("hidden")))
int fnos_home_path(const char *path, struct stat *info) {
    return in_home(path, info);
}

static int fd_home(int fd, struct stat *info) {
    char link[64], path[PATH_MAX];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path)-1);
    if (n < 0) return 0;
    path[n] = 0;
    struct stat check;
    return in_home(path, info) && !fstat(fd, &check) && check.st_dev == info->st_dev && check.st_ino == info->st_ino;
}

static void *home_acl(void) {
    struct acl *a = calloc(1, sizeof(*a));
    if (!a) return NULL;
    a->count = 1;
    a->entry.tag = 0x11; /* xattr everyone | allow, also the userspace ABI */
    a->entry.perm = ALL_RIGHTS;
    a->entry.inherit = 7; /* self, files, directories */
    errno = 0;
    return a;
}

/* Real permissions are enforced by normal Linux DAC, not synthetic metadata. */
static int shared(const struct stat *s) {
    mode_t want = S_ISDIR(s->st_mode) ? 0777 : 0666;
    return (s->st_mode & want) == want;
}
static int keep_shared(int fd);
static int user_data(const char *path);

/* POSIX default ACLs are inherited by ordinary Linux/container writers, even
 * when they use a private umask. The kernel still respects an explicit 0600
 * creation mode. Only user data directories receive inheritance; package and
 * application-private directories keep their own defaults.
 * Linux UAPI: version 2, ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_OTHER, rwx, no ID.
 */
static int inherit_shared(int fd) {
    static const unsigned char acl[] = {
        2,0,0,0,
        1,0,7,0,255,255,255,255,
        4,0,7,0,255,255,255,255,
        32,0,7,0,255,255,255,255
    };
    struct stat s;
    char link[64], path[PATH_MAX];
    if (fstat(fd, &s) || !S_ISDIR(s.st_mode)) return 0;
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path)-1);
    if (n < 0) return 0;
    path[n] = 0;
    if (!user_data(path)) return 0;
    /* Remote providers may not implement POSIX default ACLs. Existing native
     * access and mode handling must remain usable on those providers. */
    unsigned char current[sizeof(acl)];
    ssize_t size = fgetxattr(fd, "system.posix_acl_default", current, sizeof(current));
    if (size == (ssize_t)sizeof(acl) && !memcmp(current, acl, sizeof(acl)))
        return 1;
    return !fsetxattr(fd, "system.posix_acl_default", acl, sizeof(acl), 0);
}

static int ensure_shared_fd(int fd, struct stat *s) {
    if (!fd_home(fd, s)) return 0;
    if (shared(s)) { inherit_shared(fd); return 1; }
    return !keep_shared(fd) && !fstat(fd, s) && shared(s);
}

static int ensure_shared_path(const char *path, struct stat *s) {
    if (!in_home(path, s)) return 0;
    if (shared(s) && !S_ISDIR(s->st_mode)) return 1;
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return 0;
    int ok = ensure_shared_fd(fd, s), error = errno;
    close(fd); errno = error;
    return ok;
}

#define GET_PATH(name) \
void *name(const char *path) { \
    struct stat s; \
    if (ensure_shared_path(path, &s)) return home_acl(); \
    void *(*next)(const char *) = dlsym(RTLD_NEXT, #name); \
    if (next) return next(path); \
    errno = ENOSYS; return NULL; \
}
GET_PATH(trimacl_get_file)
GET_PATH(l_trimacl_get_file)

#define GET_ALL(name) \
void *name(const char *path, unsigned flags) { \
    struct stat s; \
    if (ensure_shared_path(path, &s)) return home_acl(); \
    void *(*next)(const char *, unsigned) = dlsym(RTLD_NEXT, #name); \
    if (next) return next(path, flags); \
    errno = ENOSYS; return NULL; \
}
GET_ALL(trimacl_get_file_all)
GET_ALL(l_trimacl_get_file_all)

void *trimacl_get_fd(int fd) {
    struct stat s;
    if (ensure_shared_fd(fd, &s)) return home_acl();
    void *(*next)(int) = dlsym(RTLD_NEXT, "trimacl_get_fd");
    if (next) return next(fd);
    errno = ENOSYS; return NULL;
}
void *trimacl_get_fd_all(int fd, unsigned flags) {
    struct stat s;
    if (ensure_shared_fd(fd, &s)) return home_acl();
    void *(*next)(int, unsigned) = dlsym(RTLD_NEXT, "trimacl_get_fd_all");
    if (next) return next(fd, flags);
    errno = ENOSYS; return NULL;
}

static int keep_shared(int fd) {
    struct stat s;
    if (!fd_home(fd, &s)) { errno = EPERM; return -1; }
    /* A native ACL worker may run as a household user instead of the owner.
     * Avoid requiring chmod privileges for a policy that is already applied. */
    if (shared(&s) && !(s.st_mode & (S_ISUID | S_ISGID | S_ISVTX)) &&
        (!S_ISDIR(s.st_mode) || inherit_shared(fd)))
        return 0;
    mode_t mode = (s.st_mode & 0777) | (S_ISDIR(s.st_mode) ? 0777 : 0666);
    int rc = fchmod(fd, mode), saved = errno;
    if (!rc) inherit_shared(fd);
    errno = saved; return rc;
}

#define SET_PATH(name) \
int name(const char *path, const void *acl) { \
    struct stat s; \
    if (in_home(path, &s)) { \
        int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC); \
        if (fd < 0) return -1; \
        int rc = keep_shared(fd), error = errno; close(fd); errno = error; return rc; \
    } \
    int (*next)(const char *, const void *) = dlsym(RTLD_NEXT, #name); \
    if (next) return next(path, acl); \
    errno = ENOSYS; return -1; \
}
SET_PATH(trimacl_set_file)
SET_PATH(l_trimacl_set_file)

int trimacl_set_fd(int fd, const void *acl) {
    struct stat s;
    if (fd_home(fd, &s)) return keep_shared(fd);
    int (*next)(int, const void *) = dlsym(RTLD_NEXT, "trimacl_set_fd");
    if (next) return next(fd, acl);
    errno = ENOSYS; return -1;
}

/* libnfile::is_mountpoint treats every nonzero return as a mountpoint,
 * including EBADF from its vendor-only faccessat request. Use Linux mount
 * IDs, so a same-filesystem bind mount remains protected too. */
static int home_mountpoint(const char *path, int flags) {
    char full[PATH_MAX], parent[PATH_MAX];
    struct stat link;
    struct statx current, above;
    if (flags & ~AT_SYMLINK_NOFOLLOW) { errno = EINVAL; return -1; }
    if (lstat(path, &link)) return -1;
    if ((flags & AT_SYMLINK_NOFOLLOW) && S_ISLNK(link.st_mode)) return 0;
    if (!realpath(path, full)) return -1;
    if (!strcmp(full, "/")) return 1;
    strcpy(parent, full);
    char *slash = strrchr(parent, '/');
    if (slash == parent) slash[1] = 0; else *slash = 0;
    if (statx(AT_FDCWD, full, AT_SYMLINK_NOFOLLOW, STATX_MNT_ID, &current) ||
        statx(AT_FDCWD, parent, AT_SYMLINK_NOFOLLOW, STATX_MNT_ID, &above)) return -1;
    if (!(current.stx_mask & STATX_MNT_ID) || !(above.stx_mask & STATX_MNT_ID)) {
        errno = ENOTSUP; return -1;
    }
    return current.stx_mnt_id != above.stx_mnt_id;
}

/* Native storage asks about /volN before creating or mounting it. Such a
 * path cannot satisfy in_home(), which deliberately requires mounted data.
 * Keep this discovery exception separate from the household ACL scope. */
static int automatic_volume_root(const char *path) {
    const char *setting = getenv("FNOS_HOME_DATA_ROOT");
    if (!setting || (strcmp(setting, "auto") && strcmp(setting, "/vol*"))) return 0;
    if (!path || strncmp(path, "/vol", 4) || path[4] < '1' || path[4] > '9') return 0;
    const char *p = path + 5;
    while (*p >= '0' && *p <= '9') p++;
    return *p == 0;
}

/* Native storage's child mount process inherits this opt-in bridge. Mainline
 * filesystems reject the proprietary trimacl option. Household permissions
 * are implemented by this bridge instead; retain every other option/flag. */
int mount(const char *source, const char *target, const char *filesystem,
          unsigned long flags, const void *data) {
    int (*next)(const char *, const char *, const char *, unsigned long,
                const void *) = dlsym(RTLD_NEXT, "mount");
    if (!next) { errno = ENOSYS; return -1; }
    struct stat device, directory;
    if (!automatic_volume_root(target) || !source || !filesystem || !data ||
        (strcmp(filesystem, "btrfs") && strcmp(filesystem, "ext4")) ||
        (flags & (MS_BIND | MS_MOVE | MS_REMOUNT)) ||
        strncmp(source, "/dev/", 5) || stat(source, &device) ||
        !S_ISBLK(device.st_mode) || lstat(target, &directory) ||
        !S_ISDIR(directory.st_mode))
        return next(source, target, filesystem, flags, data);

    const char *options = data;
    size_t length = strnlen(options, 4097);
    if (length > 4096) return next(source, target, filesystem, flags, data);
    char *clean = malloc(length + 1);
    if (!clean) { errno = ENOMEM; return -1; }
    size_t used = 0;
    int written = 0, removed = 0;
    const char *part = options;
    for (;;) {
        const char *end = strchr(part, ',');
        size_t size = end ? (size_t)(end - part) : strlen(part);
        if (size == 7 && !memcmp(part, "trimacl", 7)) {
            removed = 1;
        } else {
            if (written) clean[used++] = ',';
            memcpy(clean + used, part, size); used += size; written = 1;
        }
        if (!end) break;
        part = end + 1;
    }
    clean[used] = 0;
    int result = next(source, target, filesystem, flags,
                      removed ? (used ? clean : NULL) : data);
    int saved_errno = errno;
    free(clean);
    errno = saved_errno;
    return result;
}

int faccessat(int dfd, const char *path, int mode, int flags) {
    struct stat s;
    const char *target = path;
    if (dfd == -21581 && mode == F_OK && automatic_volume_root(path)) {
        if (flags & ~AT_SYMLINK_NOFOLLOW) { errno = EINVAL; return -1; }
        if (lstat(path, &s)) return errno == ENOENT ? 0 : -1;
        /* A symlink or file at a volume root is not a reusable mount target. */
        if (!S_ISDIR(s.st_mode)) return 1;
        return home_mountpoint(path, flags);
    }
    if (dfd == -21581 && in_home(path, &s)) return home_mountpoint(path, flags);
    if (dfd == -21585) {
        const struct realpath_request *r = (const struct realpath_request *)path;
        if (in_home(r->filename, &s)) return realpath(r->filename, r->resolved) ? 0 : -1;
    }
    if (dfd == -21586 || dfd == -21579 || dfd == -21577)
        target = ((const struct access_request *)path)->filename;
    if (dfd == -21576) {
        struct get_acl *g = (struct get_acl *)path;
        int match = (g->flags & (1u << 30)) ? ensure_shared_fd(g->fd, &s) : ensure_shared_path(g->filename, &s);
        if (match && shared(&s)) {
            struct acl *a = home_acl();
            if (!a) return -1;
            a->count = 1; /* xattr version 1, same four bytes for one entry */
            if (g->buffer && g->size < sizeof(*a)) { free(a); errno = ERANGE; return -1; }
            if (g->buffer) memcpy(g->buffer, a, sizeof(*a));
            free(a); return sizeof(struct acl);
        }
    } else if ((dfd == -21582 || dfd == -21575 || dfd == -21586 || dfd == -21579 || dfd == -21577)
               && ensure_shared_path(target, &s)) {
        if (dfd == -21582) return 1;
        if (dfd == -21575) return s.st_mode;
        if (dfd == -21579) return ALL_RIGHTS;
        return 0;
    }
    int (*next)(int, const char *, int, int) = dlsym(RTLD_NEXT, "faccessat");
    return next(dfd, path, mode, flags);
}

/* Native download engines use a private umask even on a shared data path.
 * Apply the selected household policy when they create user files, before
 * returning the descriptor. App-private metadata and OS paths are excluded.
 */
static int user_data(const char *path) {
    char full[PATH_MAX];
    struct stat s;
    if (!path || !realpath(path, full) || !in_home(full, &s)) return 0;
    if (getenv("FNOS_HOME_FTP_VIEW")) return 1;
    /* External disks and native remote mounts have device/share names,
     * not a local UID directory. in_home already requires a real mount. */
    if (!strncmp(full, "/vol00/", 7) || !strncmp(full, "/vol01/", 7) ||
        !strncmp(full, "/vol02/", 7)) return 1;
    if (strncmp(full, "/vol", 4)) return 0;
    const char *p = full + 4;
    while (*p >= '0' && *p <= '9') p++;
    if (*p++ != '/') return 0;
    if (!strncmp(p, "@team", 5) && (p[5] == '/' || !p[5])) return 1;
    const char *id = p;
    while (*p >= '0' && *p <= '9') p++;
    return p - id >= 4 && (*p == '/' || !*p);
}

static void share_created_fd(int fd, int dirfd, const char *path) {
    int saved = errno;
    char name[64], resolved[PATH_MAX];
    struct stat s, check;
    snprintf(name, sizeof(name), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(name, resolved, sizeof(resolved) - 1);
    const char *candidate = NULL;
    if (n >= 0) { resolved[n] = 0; candidate = resolved; }
    /* The native FTP view is a real chroot without procfs. */
    else if (getenv("FNOS_HOME_FTP_VIEW") && dirfd == AT_FDCWD) candidate = path;
    if (fd >= 0 && candidate && user_data(candidate) && !stat(candidate, &s) &&
        !fstat(fd, &check) && s.st_dev == check.st_dev && s.st_ino == check.st_ino)
    {
        fchmod(fd, (check.st_mode & 0777) | (S_ISDIR(check.st_mode) ? 0777 : 0666));
        inherit_shared(fd);
    }
    errno = saved;
}

#define OPEN_WRAPPER(name) \
int name(const char *path, int flags, ...) { \
    mode_t mode = 0; \
    if ((flags & O_CREAT) || (flags & O_TMPFILE) == O_TMPFILE) { \
        va_list ap; va_start(ap, flags); mode = va_arg(ap, unsigned); va_end(ap); \
    } \
    int (*next)(const char *, int, ...) = dlsym(RTLD_NEXT, #name); \
    if (!next) { errno = ENOSYS; return -1; } \
    int fd = next(path, flags, mode); \
    if (fd >= 0 && (flags & O_CREAT)) share_created_fd(fd, AT_FDCWD, path); \
    return fd; \
}
OPEN_WRAPPER(open)
OPEN_WRAPPER(open64)

#define OPENAT_WRAPPER(name) \
int name(int dfd, const char *path, int flags, ...) { \
    mode_t mode = 0; \
    if ((flags & O_CREAT) || (flags & O_TMPFILE) == O_TMPFILE) { \
        va_list ap; va_start(ap, flags); mode = va_arg(ap, unsigned); va_end(ap); \
    } \
    int (*next)(int, const char *, int, ...) = dlsym(RTLD_NEXT, #name); \
    if (!next) { errno = ENOSYS; return -1; } \
    int fd = next(dfd, path, flags, mode); \
    if (fd >= 0 && (flags & O_CREAT)) share_created_fd(fd, dfd, path); \
    return fd; \
}
OPENAT_WRAPPER(openat)
OPENAT_WRAPPER(openat64)

int creat(const char *path, mode_t mode) { return open(path, O_CREAT | O_WRONLY | O_TRUNC, mode); }
int creat64(const char *path, mode_t mode) { return open64(path, O_CREAT | O_WRONLY | O_TRUNC, mode); }

#define FOPEN_WRAPPER(name) \
FILE *name(const char *path, const char *mode) { \
    FILE *(*next)(const char *, const char *) = dlsym(RTLD_NEXT, #name); \
    if (!next) { errno = ENOSYS; return NULL; } \
    FILE *f = next(path, mode); \
    if (f && (*mode == 'w' || *mode == 'a')) share_created_fd(fileno(f), AT_FDCWD, path); \
    return f; \
}
FOPEN_WRAPPER(fopen)
FOPEN_WRAPPER(fopen64)

int mkdirat(int dfd, const char *path, mode_t mode) {
    int (*next)(int, const char *, mode_t) = dlsym(RTLD_NEXT, "mkdirat");
    if (!next) { errno = ENOSYS; return -1; }
    int rc = next(dfd, path, mode), saved = errno;
    if (!rc) {
        int fd = openat(dfd, path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd >= 0) { share_created_fd(fd, dfd, path); close(fd); }
    }
    errno = saved; return rc;
}

int mkdir(const char *path, mode_t mode) { return mkdirat(AT_FDCWD, path, mode); }

/* SPDX-License-Identifier: MIT
 * Portable FTP view for fnOS kernels without the proprietary /fs namespace.
 * The native daemon authenticates first, then calls chroot(/fs/UID/ftp).
 * Each authenticated child gets a private mount namespace and a real chroot
 * containing only that user's directories on mounted fnOS data volumes.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define VIEW "/run/fnos-x5-ftp"

static int volume_name(const char *p) {
    if (strncmp(p, "vol", 3) || p[3] < '1' || p[3] > '9') return 0;
    for (p += 4; *p; p++) if (*p < '0' || *p > '9') return 0;
    return 1;
}

static int root_directory(const char *p) {
    struct stat st;
    if (mkdir(p, 0755) && errno != EEXIST) return -1;
    if (lstat(p, &st) || !S_ISDIR(st.st_mode) || st.st_uid || (st.st_mode & 0022)) {
        errno = EPERM; return -1;
    }
    return 0;
}

int chroot(const char *path) {
    int (*native)(const char *) = dlsym(RTLD_NEXT, "chroot");
    if (!native) { errno = ENOSYS; return -1; }
    int rc = native(path);
    if (!rc || errno != ENOENT) return rc;
    const char *policy = getenv("FNOS_HOME_DATA_ROOT");
    if (!policy || strcmp(policy, "auto") || strncmp(path, "/fs/", 4)) return rc;
    const char *number = path + 4;
    char *end;
    errno = 0;
    unsigned long uid = strtoul(number, &end, 10);
    if (errno || number == end || *number < '1' || *number > '9' ||
        strcmp(end, "/ftp") || uid < 1000 || uid > 2147483647UL || geteuid()) {
        errno = ENOENT; return -1;
    }
    if (!getpwuid((uid_t)uid)) { errno = ENOENT; return -1; }

    /* Clone mount topology before adding the view. No host-global mounts and
     * no unjailed fallback; the kernel drops these mounts when the child exits.
     */
    struct stat system_root;
    if (stat("/", &system_root) || root_directory(VIEW)) return -1;
    if (unshare(CLONE_NEWNS) || mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) ||
        mount("fnos-x5-ftp", VIEW, "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC,
              "mode=0755,size=1m")) return -1;
    DIR *entries = opendir("/");
    if (!entries) return -1;
    struct dirent *entry;
    int count = 0, failure = 0;
    while ((entry = readdir(entries))) {
        if (!volume_name(entry->d_name) || strlen(entry->d_name) > 32) continue;
        char volume[64], source[96], target[96], user_name[24];
        struct stat vol, user;
        snprintf(volume, sizeof(volume), "/%.32s", entry->d_name);
        int volume_fd = open(volume, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (volume_fd < 0) continue;
        snprintf(user_name, sizeof(user_name), "%lu", uid);
        int user_fd = -1;
        if (!fstat(volume_fd, &vol) && vol.st_dev != system_root.st_dev)
            user_fd = openat(volume_fd, user_name, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(volume_fd);
        if (user_fd < 0) continue;
        /* A btrfs user subvolume has its own st_dev. It need not match the
         * containing volume, but must never resolve onto the OS filesystem.
         */
        if (fstat(user_fd, &user) || user.st_dev == system_root.st_dev) {
            close(user_fd); continue;
        }
        snprintf(source, sizeof(source), "/proc/self/fd/%d", user_fd);
        snprintf(target, sizeof(target), VIEW "/%.32s", entry->d_name);
        if (mkdir(target, 0755) || mount(source, target, NULL, MS_BIND, NULL) ||
            mount(NULL, target, NULL, MS_BIND | MS_REMOUNT | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL)) {
            failure = errno; close(user_fd); break;
        }
        close(user_fd);
        count++;
    }
    closedir(entries);
    if (failure || !count) { errno = failure ? failure : ENOENT; return -1; }
    rc = native(VIEW);
    if (!rc) setenv("FNOS_HOME_FTP_VIEW", "1", 1);
    return rc;
}

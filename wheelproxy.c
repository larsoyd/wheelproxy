#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/file.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/timerfd.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

enum { IN_CAP = 1 << 17, OUT_HIGH = 1 << 22, FD_CAP = 256, MAX_FDS = 28, MAX_POINTERS = 8 };
enum { K_NONE, K_REGISTRY, K_SEAT, K_POINTER };
enum { W_LISTEN, W_SIGNAL, W_CLIENT, W_SERVER, W_TIMER };
enum { AXIS_VERTICAL = 0, SOURCE_WHEEL = 0 };
enum { PTR_LEAVE = 1, PTR_AXIS = 4, PTR_FRAME = 5, PTR_SOURCE = 6, PTR_V120 = 9 };

struct opts {
    const char *listen, *upstream;
    double mult, tau_ms, tick_ms;
    bool verbose;
};

struct dir {
    int from, to;
    uint8_t in[IN_CAP];
    size_t in_len;
    uint8_t *out;
    size_t out_len, out_cap;
    int fds[FD_CAP];
    int nfds;
    uint64_t bytes;
};

struct pointer {
    uint32_t id, version, time;
    double remaining, carry, started;
    bool absorbed;
    int notches, steps;
};

struct conn;
struct watch {
    int type;
    struct conn *c;
};

struct conn {
    int id, cfd, sfd, tfd;
    bool dead, ticking;
    struct watch wc, ws, wt;
    struct dir up, down;
    uint8_t *kind;
    uint32_t *ver;
    size_t ids;
    struct pointer ptr[MAX_POINTERS];
    int nptr;
    struct timespec last_tick;
    struct conn *next;
};

static struct opts opt = {.mult = 1.0, .tau_ms = 45, .tick_ms = 4, .listen = "wayland-smooth"};
static int epfd;
static struct conn *conns;

static double mono_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static uint32_t rd32(const uint8_t *p)
{
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

static void kill_conn(struct conn *c, const char *reason)
{
    if (c->dead)
        return;
    c->dead = true;
    int notches = 0;
    for (int i = 0; i < c->nptr; i++)
        notches += c->ptr[i].notches;
    fprintf(stderr, "[conn] close id=%d reason=%s bytes_up=%llu bytes_down=%llu notches=%d\n",
            c->id, reason, (unsigned long long)c->up.bytes, (unsigned long long)c->down.bytes, notches);
}

static void put(struct conn *c, struct dir *d, const void *p, size_t n)
{
    if (d->out_len + n > d->out_cap) {
        size_t cap = d->out_cap ? d->out_cap : 1 << 16;
        while (cap < d->out_len + n)
            cap *= 2;
        uint8_t *grown = realloc(d->out, cap);
        if (!grown) {
            kill_conn(c, "out_of_memory");
            return;
        }
        d->out = grown;
        d->out_cap = cap;
    }
    memcpy(d->out + d->out_len, p, n);
    d->out_len += n;
}

static void put_event(struct conn *c, uint32_t obj, uint32_t op, const uint32_t *args, int n)
{
    uint32_t m[8] = {obj, (uint32_t)(8 + 4 * n) << 16 | op};
    memcpy(m + 2, args, 4 * n);
    put(c, &c->down, m, 8 + 4 * n);
}

static void set_kind(struct conn *c, uint32_t id, uint8_t kind, uint32_t ver)
{
    if (id >= 0xff000000u)
        return;
    if (id >= c->ids) {
        size_t n = c->ids ? c->ids : 1024;
        while (n <= id)
            n *= 2;
        uint8_t *k = realloc(c->kind, n);
        uint32_t *v = realloc(c->ver, n * sizeof *v);
        if (k)
            c->kind = k;
        if (v)
            c->ver = v;
        if (!k || !v) {
            kill_conn(c, "out_of_memory");
            return;
        }
        memset(c->kind + c->ids, 0, n - c->ids);
        c->ids = n;
    }
    c->kind[id] = kind;
    c->ver[id] = ver;
}

static uint8_t kind_of(struct conn *c, uint32_t id)
{
    return id < c->ids ? c->kind[id] : K_NONE;
}

static struct pointer *find_pointer(struct conn *c, uint32_t id)
{
    for (int i = 0; i < c->nptr; i++)
        if (c->ptr[i].id == id)
            return &c->ptr[i];
    return NULL;
}

static void drop_pointer(struct conn *c, uint32_t id)
{
    struct pointer *p = find_pointer(c, id);
    if (p)
        *p = c->ptr[--c->nptr];
}

static void on_request(struct conn *c, const uint8_t *m, uint32_t size)
{
    uint32_t obj = rd32(m), op = rd32(m + 4) & 0xffff;
    if (obj == 1 && op == 1 && size >= 12) {
        set_kind(c, rd32(m + 8), K_REGISTRY, 0);
    } else if (kind_of(c, obj) == K_REGISTRY && op == 0 && size >= 16) {
        uint32_t len = rd32(m + 12), padded = (len + 3) & ~3u;
        if (len == 0 || 16 + padded + 8 > size)
            return;
        const char *iface = (const char *)m + 16;
        bool seat = len == sizeof "wl_seat" && memcmp(iface, "wl_seat", len) == 0;
        set_kind(c, rd32(m + 20 + padded), seat ? K_SEAT : K_NONE, rd32(m + 16 + padded));
    } else if (kind_of(c, obj) == K_SEAT && op == 0 && size >= 12) {
        uint32_t id = rd32(m + 8);
        set_kind(c, id, K_POINTER, c->ver[obj]);
        if (c->nptr < MAX_POINTERS)
            c->ptr[c->nptr++] = (struct pointer){.id = id, .version = c->ver[obj]};
    }
}

static void arm(struct conn *c, bool on)
{
    long ns = on ? (long)(opt.tick_ms * 1e6) : 0;
    struct itimerspec its = {.it_interval = {ns / 1000000000L, ns % 1000000000L}};
    its.it_value = its.it_interval;
    timerfd_settime(c->tfd, 0, &its, NULL);
    c->ticking = on;
}

static int step(struct pointer *p, double dt)
{
    double want = p->remaining * (1 - exp(-dt / opt.tau_ms));
    if (fabs(want) < 1)
        want = copysign(fmin(1, fabs(p->remaining)), p->remaining);
    p->remaining -= want;
    double total = want + p->carry;
    int n = (int)lround(total);
    p->carry = p->remaining ? total - n : 0;
    return n;
}

static void tick(struct conn *c, double dt)
{
    bool busy = false;
    for (int i = 0; i < c->nptr; i++) {
        struct pointer *p = &c->ptr[i];
        if (!p->remaining)
            continue;
        int n = step(p, dt);
        busy |= p->remaining != 0;
        p->steps += n != 0;
        if (!p->remaining && opt.verbose)
            fprintf(stderr, "[wheel] settled pointer=%u steps=%d ms=%.1f\n", p->id, p->steps, mono_ms() - p->started);
        if (!n)
            continue;
        p->time += (uint32_t)dt;
        put_event(c, p->id, PTR_SOURCE, (uint32_t[]){SOURCE_WHEEL}, 1);
        put_event(c, p->id, PTR_V120, (uint32_t[]){AXIS_VERTICAL, (uint32_t)n}, 2);
        put_event(c, p->id, PTR_AXIS, (uint32_t[]){p->time, AXIS_VERTICAL, (uint32_t)(n * 32)}, 3);
        put_event(c, p->id, PTR_FRAME, NULL, 0);
    }
    if (busy != c->ticking)
        arm(c, busy);
}

static void absorb(struct pointer *p, int32_t v120)
{
    double add = v120 * opt.mult;
    if (p->remaining && (add > 0) != (p->remaining > 0))
        p->remaining = p->carry = 0;
    if (!p->remaining) {
        p->started = mono_ms();
        p->steps = 0;
    }
    p->remaining += add;
    p->absorbed = true;
    p->notches++;
    if (opt.verbose)
        fprintf(stderr, "[wheel] pointer=%u v120=%d remaining=%.1f\n", p->id, v120, p->remaining);
}

static bool on_pointer_event(struct conn *c, struct pointer *p, const uint8_t *m, uint32_t size)
{
    uint32_t op = rd32(m + 4) & 0xffff;
    if (op == PTR_V120 && size >= 16 && p->version >= 8 && rd32(m + 8) == AXIS_VERTICAL) {
        absorb(p, (int32_t)rd32(m + 12));
        return false;
    }
    if (op == PTR_AXIS && size >= 20) {
        p->time = rd32(m + 8);
        return !(p->absorbed && rd32(m + 12) == AXIS_VERTICAL);
    }
    if (op == PTR_LEAVE)
        p->remaining = p->carry = 0;
    if (op == PTR_FRAME && p->absorbed) {
        p->absorbed = false;
        put(c, &c->down, m, size);
        if (!c->ticking) {
            clock_gettime(CLOCK_MONOTONIC, &c->last_tick);
            tick(c, opt.tick_ms);
        }
        return false;
    }
    return true;
}

static void on_event(struct conn *c, const uint8_t *m, uint32_t size)
{
    uint32_t obj = rd32(m), op = rd32(m + 4) & 0xffff;
    if (obj == 1 && op == 1 && size >= 12) {
        uint32_t id = rd32(m + 8);
        if (kind_of(c, id) == K_POINTER)
            drop_pointer(c, id);
        set_kind(c, id, K_NONE, 0);
    } else if (kind_of(c, obj) == K_POINTER) {
        struct pointer *p = find_pointer(c, obj);
        if (p && !on_pointer_event(c, p, m, size))
            return;
    }
    put(c, &c->down, m, size);
}

static void flush(struct conn *c, struct dir *d)
{
    while (d->out_len && !c->dead) {
        int nf = d->nfds < MAX_FDS ? d->nfds : MAX_FDS;
        char cbuf[CMSG_SPACE(sizeof(int) * MAX_FDS)] = {0};
        struct iovec iov = {d->out, d->out_len};
        struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1};
        if (nf) {
            mh.msg_control = cbuf;
            mh.msg_controllen = CMSG_SPACE(sizeof(int) * nf);
            struct cmsghdr *cm = CMSG_FIRSTHDR(&mh);
            cm->cmsg_level = SOL_SOCKET;
            cm->cmsg_type = SCM_RIGHTS;
            cm->cmsg_len = CMSG_LEN(sizeof(int) * nf);
            memcpy(CMSG_DATA(cm), d->fds, sizeof(int) * nf);
        }
        ssize_t n = sendmsg(d->to, &mh, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n < 0) {
            if (errno != EAGAIN && errno != EINTR)
                kill_conn(c, d == &c->up ? "send_upstream" : "send_client");
            return;
        }
        for (int i = 0; i < nf; i++)
            close(d->fds[i]);
        memmove(d->fds, d->fds + nf, sizeof(int) * (d->nfds - nf));
        d->nfds -= nf;
        memmove(d->out, d->out + n, d->out_len - n);
        d->out_len -= n;
        d->bytes += n;
    }
}

static void interest(struct conn *c)
{
    struct epoll_event ec = {.events = (c->up.out_len < OUT_HIGH ? EPOLLIN : 0) | (c->down.out_len ? EPOLLOUT : 0),
                             .data.ptr = &c->wc};
    struct epoll_event es = {.events = (c->down.out_len < OUT_HIGH ? EPOLLIN : 0) | (c->up.out_len ? EPOLLOUT : 0),
                             .data.ptr = &c->ws};
    epoll_ctl(epfd, EPOLL_CTL_MOD, c->cfd, &ec);
    epoll_ctl(epfd, EPOLL_CTL_MOD, c->sfd, &es);
}

static void pump(struct conn *c, struct dir *d)
{
    char cbuf[CMSG_SPACE(sizeof(int) * MAX_FDS)];
    struct iovec iov = {d->in + d->in_len, IN_CAP - d->in_len};
    struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf, .msg_controllen = sizeof cbuf};
    ssize_t n = recvmsg(d->from, &mh, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (n < 0 && (errno == EAGAIN || errno == EINTR))
        return;
    if (n <= 0) {
        kill_conn(c, d == &c->up ? "client_hangup" : "upstream_hangup");
        return;
    }
    for (struct cmsghdr *cm = CMSG_FIRSTHDR(&mh); cm; cm = CMSG_NXTHDR(&mh, cm)) {
        if (cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS)
            continue;
        int nf = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);
        for (int i = 0; i < nf; i++) {
            int fd;
            memcpy(&fd, CMSG_DATA(cm) + i * sizeof(int), sizeof fd);
            if (d->nfds == FD_CAP) {
                close(fd);
                kill_conn(c, "fd_queue_full");
                continue;
            }
            d->fds[d->nfds++] = fd;
        }
    }
    d->in_len += n;
    size_t off = 0;
    while (d->in_len - off >= 8 && !c->dead) {
        uint32_t size = rd32(d->in + off + 4) >> 16;
        if (size < 8 || size % 4) {
            fprintf(stderr, "[proto] error id=%d reason=bad_size size=%u obj=%u\n", c->id, size, rd32(d->in + off));
            kill_conn(c, "bad_message");
            return;
        }
        if (d->in_len - off < size)
            break;
        if (d == &c->up) {
            on_request(c, d->in + off, size);
            put(c, d, d->in + off, size);
        } else {
            on_event(c, d->in + off, size);
        }
        off += size;
    }
    memmove(d->in, d->in + off, d->in_len - off);
    d->in_len -= off;
}

static const char *runtime_path(const char *name, char *buf, size_t n)
{
    const char *dir = getenv("XDG_RUNTIME_DIR");
    if (name[0] == '/')
        snprintf(buf, n, "%s", name);
    else
        snprintf(buf, n, "%s/%s", dir ? dir : "", name);
    return buf;
}

static void add_watch(int fd, struct watch *w, uint32_t events)
{
    struct epoll_event ev = {.events = events, .data.ptr = w};
    epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev);
}

static void accept_client(int lfd)
{
    static int next_id;
    int cfd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (cfd < 0)
        return;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", runtime_path(opt.upstream, path, sizeof path));
    int sfd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (sfd < 0 || connect(sfd, (struct sockaddr *)&sa, sizeof sa) < 0) {
        fprintf(stderr, "[conn] refused reason=upstream_connect path=%s errno=%s\n", sa.sun_path, strerror(errno));
        close(cfd);
        if (sfd >= 0)
            close(sfd);
        return;
    }
    fcntl(sfd, F_SETFL, O_NONBLOCK);
    struct conn *c = calloc(1, sizeof *c);
    if (!c) {
        close(cfd);
        close(sfd);
        return;
    }
    *c = (struct conn){.id = ++next_id, .cfd = cfd, .sfd = sfd, .next = conns};
    c->tfd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    c->up.from = c->down.to = cfd;
    c->up.to = c->down.from = sfd;
    c->wc = (struct watch){W_CLIENT, c};
    c->ws = (struct watch){W_SERVER, c};
    c->wt = (struct watch){W_TIMER, c};
    conns = c;
    add_watch(cfd, &c->wc, EPOLLIN);
    add_watch(sfd, &c->ws, EPOLLIN);
    add_watch(c->tfd, &c->wt, EPOLLIN);
    fprintf(stderr, "[conn] open id=%d upstream=%s\n", c->id, sa.sun_path);
}

static void free_dir(struct dir *d)
{
    for (int i = 0; i < d->nfds; i++)
        close(d->fds[i]);
    free(d->out);
}

static void reap(void)
{
    for (struct conn **pp = &conns; *pp;) {
        struct conn *c = *pp;
        if (!c->dead) {
            pp = &c->next;
            continue;
        }
        *pp = c->next;
        close(c->cfd);
        close(c->sfd);
        close(c->tfd);
        free_dir(&c->up);
        free_dir(&c->down);
        free(c->kind);
        free(c->ver);
        free(c);
    }
}

static void on_timer(struct conn *c)
{
    uint64_t expirations;
    if (read(c->tfd, &expirations, sizeof expirations) != sizeof expirations)
        return;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    double dt = (ts.tv_sec - c->last_tick.tv_sec) * 1e3 + (ts.tv_nsec - c->last_tick.tv_nsec) / 1e6;
    c->last_tick = ts;
    tick(c, dt);
}

static bool parse_num(const char *flag, const char *s, double lo, double *out)
{
    char *end;
    errno = 0;
    double v = s ? strtod(s, &end) : 0;
    if (!s || errno || *end || !(v >= lo) || !isfinite(v)) {
        fprintf(stderr, "[args] refused reason=bad_number flag=%s value=%s min=%g\n", flag, s ? s : "(none)", lo);
        return false;
    }
    *out = v;
    return true;
}

static bool parse_args(int argc, char **argv)
{
    opt.upstream = getenv("WAYLAND_DISPLAY");
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
        bool ok = true;
        if (!strcmp(a, "--verbose")) {
            opt.verbose = true;
            continue;
        } else if (!strcmp(a, "--listen") && v) {
            opt.listen = v;
        } else if (!strcmp(a, "--upstream") && v) {
            opt.upstream = v;
        } else if (!strcmp(a, "--mult")) {
            ok = parse_num(a, v, 0.01, &opt.mult);
        } else if (!strcmp(a, "--tau-ms")) {
            ok = parse_num(a, v, 1, &opt.tau_ms);
        } else if (!strcmp(a, "--tick-ms")) {
            ok = parse_num(a, v, 1, &opt.tick_ms);
        } else {
            fprintf(stderr, "[args] refused reason=unknown_flag flag=%s\n", a);
            return false;
        }
        if (!ok)
            return false;
        i++;
    }
    if (!opt.upstream || !*opt.upstream || !strcmp(opt.upstream, opt.listen)) {
        fprintf(stderr, "[args] refused reason=bad_upstream upstream=%s listen=%s\n",
                opt.upstream ? opt.upstream : "(unset)", opt.listen);
        return false;
    }
    if (!getenv("XDG_RUNTIME_DIR") && (opt.listen[0] != '/' || opt.upstream[0] != '/')) {
        fprintf(stderr, "[args] refused reason=no_runtime_dir\n");
        return false;
    }
    return true;
}

static int open_listener(char *path, size_t n)
{
    char lock[sizeof(((struct sockaddr_un *)0)->sun_path) + 8];
    runtime_path(opt.listen, path, n);
    snprintf(lock, sizeof lock, "%s.lock", path);
    int lfd_lock = open(lock, O_CREAT | O_RDWR | O_CLOEXEC, 0640);
    if (lfd_lock < 0 || flock(lfd_lock, LOCK_EX | LOCK_NB) < 0) {
        fprintf(stderr, "[listen] refused reason=lock path=%s errno=%s\n", lock, strerror(errno));
        return -1;
    }
    unlink(path);
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", path);
    int lfd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (lfd < 0 || bind(lfd, (struct sockaddr *)&sa, sizeof sa) < 0 || listen(lfd, 16) < 0) {
        fprintf(stderr, "[listen] refused reason=bind path=%s errno=%s\n", path, strerror(errno));
        return -1;
    }
    return lfd;
}

int main(int argc, char **argv)
{
    if (!parse_args(argc, argv))
        return 2;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    int lfd = open_listener(path, sizeof path);
    if (lfd < 0)
        return 1;
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    signal(SIGPIPE, SIG_IGN);
    int sfd = signalfd(-1, &mask, SFD_CLOEXEC);
    epfd = epoll_create1(EPOLL_CLOEXEC);
    struct watch wl = {W_LISTEN, NULL}, wsig = {W_SIGNAL, NULL};
    add_watch(lfd, &wl, EPOLLIN);
    add_watch(sfd, &wsig, EPOLLIN);
    fprintf(stderr, "[wheelproxy] start listen=%s upstream=%s mult=%g tau_ms=%g tick_ms=%g\n",
            path, opt.upstream, opt.mult, opt.tau_ms, opt.tick_ms);
    for (bool running = true; running;) {
        struct epoll_event evs[32];
        int n = epoll_wait(epfd, evs, 32, 1000);
        for (int i = 0; i < n; i++) {
            struct watch *w = evs[i].data.ptr;
            struct conn *c = w->c;
            if (w->type == W_LISTEN) {
                accept_client(lfd);
                continue;
            }
            if (w->type == W_SIGNAL) {
                running = false;
                continue;
            }
            if (c->dead)
                continue;
            if (w->type == W_TIMER)
                on_timer(c);
            else if (evs[i].events & (EPOLLIN | EPOLLHUP | EPOLLERR))
                pump(c, w->type == W_CLIENT ? &c->up : &c->down);
            flush(c, &c->up);
            flush(c, &c->down);
            if (!c->dead)
                interest(c);
        }
        reap();
    }
    for (struct conn *c = conns; c; c = c->next)
        kill_conn(c, "shutdown");
    reap();
    unlink(path);
    fprintf(stderr, "[wheelproxy] stop\n");
    return 0;
}
